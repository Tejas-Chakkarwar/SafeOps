"""Experiment runner: one run of the §5 sequence for one experimental unit.

Each step calls the component that owns it. Wherever the runner itself has to interpret
a result (discard on a dirty cluster, route on proposal_status, and so on), it calls one
of its own stubbed hooks. Until that hook is implemented, an implemented stage cannot be
silently ignored: the run stops loudly at the hook instead.

Keep this call order in step with ``safeops.experiment.sequence.RUN_SEQUENCE``.
tests/test_run_sequence.py enforces that.
"""

from __future__ import annotations

from datetime import UTC, datetime

from safeops.agent.loop import Agent
from safeops.agent.parser import ProposalParser
from safeops.cluster import CommandError
from safeops.environment.manager import CleanStateResult, EnvironmentManager, QuarantineFailed
from safeops.environment.workload import (
    SteadyStateError,
    WorkloadGenerator,
    WorkloadGeneratorBusy,
    check_steady_state,
)
from safeops.executor.executor import ActionExecutor
from safeops.experiment.records import RECORD_FIELDS, Discard, RunRecordWriter
from safeops.experiment.unit import RunUnit
from safeops.faults.alerts import AlertGenerator
from safeops.faults.injector import (
    FaultInjectionError,
    FaultInjector,
    FaultNotPresent,
    FaultVerificationFailed,
)
from safeops.features.extractor import RiskFeatureExtractor
from safeops.gate.always_act import AlwaysActGate
from safeops.gate.shadow import ShadowGateScorer
from safeops.stubs import NotImplementedStage, stub
from safeops.verify.effectiveness import EffectivenessVerifier
from safeops.verify.harm import HarmChecker


class PostAgentDiscard(Exception):
    """A harness or verifier failure after the agent started. The run was recorded as
    discarded with all its data, cleaned up, and recovered if dirty (decision 0017 §1)."""


class PreRunDiscard(Exception):
    """The environment wasn't clean before the run. The run is discarded and counted
    (§11.1), and the worker was quarantined and rebuilt."""


__all__ = ["ExperimentRunner", "PostAgentDiscard", "PreRunDiscard", "RunUnit", "build_runner"]


class ExperimentRunner:
    def __init__(
        self,
        *,
        environment: EnvironmentManager,
        workload: WorkloadGenerator,
        injector: FaultInjector,
        alerts: AlertGenerator,
        agent: Agent,
        parser: ProposalParser,
        extractor: RiskFeatureExtractor,
        shadow_gates: ShadowGateScorer,
        gate: AlwaysActGate,
        executor: ActionExecutor,
        effectiveness: EffectivenessVerifier,
        harm: HarmChecker,
        records: RunRecordWriter,
    ) -> None:
        self.environment = environment
        self.workload = workload
        self.injector = injector
        self.alerts = alerts
        self.agent = agent
        self.parser = parser
        self.extractor = extractor
        self.shadow_gates = shadow_gates
        self.gate = gate
        self.executor = executor
        self.effectiveness = effectiveness
        self.harm = harm
        self.records = records
        self._halt: Exception | None = None
        self._topology = None
        self._wall_clock = lambda: datetime.now(UTC)

    def run(self, unit: RunUnit) -> None:
        """One attempted run. Exactly one record per attempted unit (decision 0017 §1, R1),
        and once the agent has produced a trajectory or an action was attempted, a record
        however the run ends (R2)."""
        env = self.environment

        if self._halt is not None:  # a failed quarantine halted this worker (review L2)
            raise self._halt

        # Provenance as of now, when the code that runs is the code recorded (M5 review,
        # finding 3). If it can't be captured, nothing was attempted: no record (R1).
        data: dict = {
            "unit": unit,
            "started_at": self._wall_clock(),
            "provenance": self.records.capture_provenance(unit),
            "action_attempted": False,
        }

        # Pre-run. A reset that fails outright counts as a dirty environment (review M2).
        # One that can't produce a result at all (a configuration, guard or credential
        # error, a quarantined worker) propagates: nothing was attempted, so no record (R1).
        try:
            env.reset_environment()
            pre_clean = env.verify_clean_state()
        except CommandError as exc:
            pre_clean = CleanStateResult(False, (f"pre-run reset failed: {exc}",))
        data["clean_state_verified"] = pre_clean.clean
        try:
            self._require_pre_run_clean(pre_clean)
        except PreRunDiscard as exc:
            # The record first, then the rebuild (same order as post-run; decision 0013). A
            # record failure never skips the rebuild (M5 second review, finding 3).
            path = self._finalize_then_recover(
                data,
                Discard("pre_run_clean_state", "pre_run_dirty", str(exc)),
                pre_clean,
                None,
                recover=lambda: self._quarantine("pre-run: " + "; ".join(pre_clean.problems[:10])),
            )
            self._note_record(exc, path)
            raise

        # From the start of the load until the measurements are complete, any failure stops
        # the load, so it never leaves the generator busy for the next run. That holds for a
        # pre-agent discard (Milestone 4 review, finding 3), a stub or crash after the agent
        # starts (second review, finding 1), Ctrl-C (finding 5), and a start whose control
        # response was lost: the generator may be running anyway (fix list, item 4). The one
        # exception is WorkloadGeneratorBusy, which means the load is someone else's (0009,
        # W5). Then _handle_failure writes the record R1/R2 call for (decision 0017 §1).
        stage = "start_workload"
        try:
            self.workload.start_workload()
            stage = "steady_state"
            steady_state = data["steady_state"] = self.workload.verify_steady_state()
            stage = "steady_state_baseline"  # the check, after the window is recorded
            self._require_steady_state(steady_state)
            stage = "prefault_snapshot"
            data["prefault"] = env.capture_snapshot()
            stage = "inject"
            fault = data["fault"] = self.injector.inject(unit.fault_id, unit.fault_seed)
            stage = "verify_fault_present"
            verification = data["fault_verification"] = self.injector.verify_fault_present(fault)
            stage = "fault_present"
            self._require_fault_present(verification)
            # Harm baseline (§5, Rule 2), taken once the projection has settled; flagged if
            # it never settled, never silently a mid-transition snapshot (decision 0013).
            stage = "postfault_snapshot"
            postfault = data["postfault"] = env.capture_settled_snapshot()

            # Information wall (§6): only the alert, built from the verified symptom,
            # crosses into the runtime. The fault and its spec stay on this side.
            stage = "alert"
            alert = data["alert"] = self.alerts.build_alert(verification)
            stage = "runtime_context"
            context = self._runtime_context(alert)
            stage = "agent"
            raw_output, trajectory = self.agent.investigate(context)
            data["trajectory"] = trajectory  # from here on, R2: a record however it ends
            stage = "parse"
            proposal = data["proposal"] = self.parser.parse(raw_output)

            executed_action = None
            stage = "route_proposal"
            action = self._gate_eligible_action(proposal)
            if action is not None:
                stage = "extract"
                features = data["features"] = self.extractor.extract(proposal, trajectory, context)
                stage = "shadow_gates"
                data["shadow_scores"] = self.shadow_gates.score(features)
                stage = "gate"
                decision = data["decision"] = self.gate.decide(features)
                stage = "route_gate"
                if self._should_execute(decision):
                    stage = "execute"
                    data["action_attempted"] = True  # before the call: it may raise mid-way
                    execution = data["execution"] = self.executor.apply(action)
                    stage = "executed_action"
                    executed_action = self._executed_action(action, execution)

            # Verification, against the post-fault snapshot (§11.7).
            stage = "stabilization"
            data["stabilized"] = env.wait_stabilization_window()
            stage = "postaction_snapshot"
            postaction = data["postaction"] = env.capture_snapshot()
            stage = "effectiveness"
            effective = self.effectiveness.verify(steady_state)
            stage = "harm"
            harm = self.harm.check(executed_action, context, postfault.snapshot, postaction)
            data["effective"], data["harm"] = effective, harm
        except BaseException as exc:
            if not isinstance(exc, WorkloadGeneratorBusy):
                self._stop_load_after_failure(exc)
            self._handle_failure(data, stage, exc)
            raise  # _handle_failure always raises; this is never reached

        # Measurements are complete. Everything from here on is cleanup, and no cleanup
        # failure may erase the run (§11.1; Milestone 3 third review). Order:
        #   1. clean up: stop the workload, reset, verify (failures become "not clean")
        #   2. determine reset_verified
        #   3. finalize the record
        #   4. only then recover a dirty environment (quarantine + rebuild)
        #   5. a failed recovery halts the worker, after the record exists
        post_clean, interrupt = self._cleanup()
        data["reset_verified"] = self._post_run_clean(post_clean)
        self._finalize_then_recover(data, None, post_clean, interrupt)
        if interrupt is not None:
            raise interrupt

    def _finalize_then_recover(self, data, discard, post_clean, interrupt, recover=None):
        """Write the record, then recover a dirty environment (strictly in that order).

        A record that can't be written never stops recovery, and a recovery that fails
        never hides the record failure: it is attached as a note (M5 review, finding 7).
        Recovery is skipped after a second Ctrl-C; the next pre-run check finds the dirt.
        """
        record_error: Exception | None = None
        path = None
        try:
            path = self._finalize(data, discard)
        except Exception as exc:
            record_error = exc
        if interrupt is None:
            try:
                if recover is None:
                    self._recover_if_dirty(post_clean)
                else:
                    recover()
            except BaseException as recovery_exc:
                if record_error is not None:
                    recovery_exc.add_note(
                        f"the run record also failed: {type(record_error).__name__}: {record_error}"
                    )
                raise
        if record_error is not None:
            raise record_error
        return path

    def _cleanup(self) -> tuple[CleanStateResult, BaseException | None]:
        """Stop the load, reset, verify. Never raises: every failure becomes "not clean".

        A second Ctrl-C during cleanup is remembered rather than raised at once, so the
        record is still written; the caller then skips recovery and re-raises it (the
        next run's pre-run check will find the environment dirty).
        """
        env = self.environment
        problems: list[str] = []
        interrupt: BaseException | None = None
        try:
            self.workload.stop_workload()
        except BaseException as exc:
            problems.append(f"stop_workload failed: {type(exc).__name__}: {exc}")
            interrupt = None if isinstance(exc, Exception) else exc
        try:
            env.reset_environment()
            post_clean = env.verify_clean_state()
        except BaseException as exc:  # any failure here (even an expired credential)
            post_clean = CleanStateResult(
                False, (f"post-run reset failed: {type(exc).__name__}: {exc}",)
            )
            interrupt = interrupt or (None if isinstance(exc, Exception) else exc)
        if problems:
            post_clean = CleanStateResult(False, tuple(problems) + post_clean.problems)
        return post_clean, interrupt

    def _finalize(self, data: dict, discard: Discard | None = None):
        fields = {k: v for k, v in data.items() if k in RECORD_FIELDS}
        return self.records.finalize(**fields, discard=discard)

    @staticmethod
    def _note_record(exc: BaseException, path) -> None:
        if path is not None:
            exc.add_note(f"run record: {path}")

    def _handle_failure(self, data: dict, stage: str, exc: BaseException) -> None:
        """Apply R1/R2 (decision 0017 §1) to a failure after the pre-run check. Always raises.

        - A stub (NotImplementedStage) or an abort (Ctrl-C) before R2 applies is not an
          attempted experimental outcome: no record.
        - A pre-agent failure is a discard record. The injected fault, if any, is left for
          the next run's pre-run reset (decision 0015).
        - From the agent on, the run is cleaned up, recorded as discarded with all its data,
          then recovered if dirty, in that order. Its exception surfaces as PostAgentDiscard.
        """
        r2 = data.get("trajectory") is not None or data["action_attempted"]
        aborted = not isinstance(exc, Exception)
        if not r2 and (aborted or isinstance(exc, NotImplementedStage)):
            raise exc
        discard = Discard(stage, _cause(exc, data, aborted), f"{type(exc).__name__}: {exc}")
        if discard.phase == "pre_agent":
            self._note_record(exc, self._finalize(data, discard))
            raise exc
        post_clean, interrupt = self._cleanup()
        data["reset_verified"] = self._post_run_clean(post_clean)
        path = self._finalize_then_recover(data, discard, post_clean, interrupt)
        if aborted or isinstance(exc, NotImplementedStage) or interrupt is not None:
            self._note_record(exc, path)
            raise exc
        discarded = PostAgentDiscard(
            f"discarded after the agent started (stage {stage}); recorded with all its data:"
            f" {type(exc).__name__}: {exc}"
        )
        self._note_record(discarded, path)
        raise discarded from exc

    def _quarantine(self, reason: str) -> None:
        """Quarantine and rebuild. A failure halts the worker: it is kept, and raised."""
        try:
            self.environment.quarantine_and_rebuild(reason=reason)
        except Exception as exc:
            self._halt = (
                exc
                if isinstance(exc, QuarantineFailed)
                else QuarantineFailed(f"quarantine failed: {type(exc).__name__}: {exc}")
            )
            raise self._halt from exc

    def _stop_load_after_failure(self, exc: BaseException) -> None:
        """Best-effort stop of the load this run started. A stop failure is attached to the
        original exception, never raised in its place."""
        try:
            self.workload.stop_workload()
        except Exception as stop_exc:
            exc.add_note(f"stop_workload after the failure also failed: {stop_exc}")

    # Runner hooks: points where the runner interprets a stage's result.

    def _require_pre_run_clean(self, clean_state) -> None:
        """If not clean, the run is a pre-run discard (§5, §11.1): raise PreRunDiscard.

        Pure. The runner then writes the discard record and only after that quarantines
        and rebuilds (record first, as post-run; decisions 0013, 0017 §1). A rebuild that
        fails raises QuarantineFailed and halts the worker.
        """
        if clean_state.clean:
            return
        reason = "; ".join(clean_state.problems[:10])
        raise PreRunDiscard(
            f"environment not clean before the run; discarded and rebuilt: {reason}"
        )

    def _require_steady_state(self, steady_state) -> None:
        """Stop the run if no valid steady-state baseline was measured (decision 0009).

        Raises SteadyStateError, a pre-run failure: the run is discarded and counted.
        Recording the discard is Milestone 5.
        """
        check_steady_state(steady_state)

    def _require_fault_present(self, verification) -> None:
        """Silent non-injection: discard before the agent starts (§11.2).

        Raises FaultNotPresent, a pre-run discard. Recording the discard is Milestone 5.
        """
        if not verification.present:
            raise FaultNotPresent(
                f"silent non-injection; run discarded before the agent: {verification.reason}"
            )

    def _runtime_context(self, alert):
        """Build IncidentRuntimeContext(alert, topology, now). It holds no fault spec (§6)."""
        from safeops.context import IncidentRuntimeContext

        if self._topology is None:
            from safeops.topology import load_topology

            self._topology = load_topology()
        return IncidentRuntimeContext(alert=alert, topology=self._topology, now=self._wall_clock())

    @stub(milestone=8)
    def _gate_eligible_action(self, proposal):
        """Return the proposed action if proposal_status == valid_action, else None (§7.1)."""

    @stub(milestone=8)
    def _should_execute(self, decision) -> bool:
        """True if the live gate's decision is execute."""

    @stub(milestone=8)
    def _executed_action(self, action, execution):
        """Return the action the harm checker should judge, given the execution status."""

    def _post_run_clean(self, clean_state) -> bool:
        """Return reset_verified for the record. Pure: no recovery happens here.

        A completed run is never discarded for a failed cleanup: the hardest runs to clean
        up are the ones where the agent did the most damage (§11.1).
        """
        return bool(clean_state.clean)

    def _recover_if_dirty(self, clean_state) -> None:
        """After the record is finalized: quarantine and rebuild a dirty environment.

        If recovery fails, the worker halts: QuarantineFailed is raised here and kept, so
        the runner refuses later runs. The record already exists by then.
        """
        if clean_state.clean:
            return
        self._quarantine("post-run: " + "; ".join(clean_state.problems[:10]))


def _cause(exc: BaseException, data: dict, aborted: bool) -> str:
    """The discard cause recorded for a failure (decision 0017 §1)."""
    if aborted:
        return "operator_abort"
    if isinstance(exc, NotImplementedStage):
        return "not_implemented"
    if isinstance(exc, WorkloadGeneratorBusy):
        return "workload_generator_busy"
    if isinstance(exc, SteadyStateError) and data.get("steady_state") is not None:
        return "no_steady_state"
    if isinstance(exc, FaultInjectionError):
        return "injection_failed"
    if isinstance(exc, FaultVerificationFailed):
        return "detection_unmeasurable"
    if isinstance(exc, FaultNotPresent):
        return "silent_non_injection"
    return "infrastructure_failure"


def build_runner() -> ExperimentRunner:
    """Wire the default components. Constructing them has no side effects."""
    from safeops.faults.spec import FAULTS_DIR

    workload = WorkloadGenerator()  # shared: the injector measures on the running workload
    return ExperimentRunner(
        environment=EnvironmentManager(),
        workload=workload,
        injector=FaultInjector(workload=workload, faults_dir=FAULTS_DIR),
        alerts=AlertGenerator(),
        agent=Agent(),
        parser=ProposalParser(),
        extractor=RiskFeatureExtractor(),
        shadow_gates=ShadowGateScorer(),
        gate=AlwaysActGate(),
        executor=ActionExecutor(),
        effectiveness=EffectivenessVerifier(),
        harm=HarmChecker(),
        records=RunRecordWriter(faults_dir=FAULTS_DIR),  # one spec source for both
    )
