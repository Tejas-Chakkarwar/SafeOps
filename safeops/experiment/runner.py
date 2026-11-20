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
    WorkloadGenerator,
    WorkloadGeneratorBusy,
    check_steady_state,
)
from safeops.executor.executor import ActionExecutor
from safeops.experiment.records import RunRecordWriter
from safeops.experiment.unit import RunUnit
from safeops.faults.alerts import AlertGenerator
from safeops.faults.injector import FaultInjector, FaultNotPresent
from safeops.features.extractor import RiskFeatureExtractor
from safeops.gate.always_act import AlwaysActGate
from safeops.gate.shadow import ShadowGateScorer
from safeops.stubs import stub
from safeops.verify.effectiveness import EffectivenessVerifier
from safeops.verify.harm import HarmChecker


class PreRunDiscard(Exception):
    """The environment wasn't clean before the run. The run is discarded and counted
    (§11.1), and the worker was quarantined and rebuilt."""


__all__ = ["ExperimentRunner", "PreRunDiscard", "RunUnit", "build_runner"]


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
        env = self.environment

        if self._halt is not None:  # a failed quarantine halted this worker (review L2)
            raise self._halt

        # Pre-run. A failure here is discardable (§7.1, §11.1). A reset that fails outright
        # counts as a dirty environment: quarantine, then discard (review M2).
        try:
            env.reset_environment()
            pre_clean = env.verify_clean_state()
        except CommandError as exc:
            pre_clean = CleanStateResult(False, (f"pre-run reset failed: {exc}",))
        self._require_pre_run_clean(pre_clean)
        # From the start of the load until the measurements are complete, any failure stops
        # the load, so it never leaves the generator busy for the next run. That holds for a
        # pre-agent discard (Milestone 4 review, finding 3), a stub or crash after the agent
        # starts (second review, finding 1), Ctrl-C (finding 5), and a start whose control
        # response was lost: the generator may be running anyway (fix list, item 4). The one
        # exception is WorkloadGeneratorBusy, which means the load is someone else's (0009,
        # W5). This is cleanup, not classification: how such a run is recorded is Milestone
        # 5, and an injected fault is left for the next run's pre-run reset.
        try:
            self.workload.start_workload()
            steady_state = self.workload.verify_steady_state()
            self._require_steady_state(steady_state)
            prefault = env.capture_snapshot()
            fault = self.injector.inject(unit.fault_id, unit.fault_seed)
            verification = self.injector.verify_fault_present(fault)
            self._require_fault_present(verification)
            # Harm baseline (§5, Rule 2), taken once the projection has settled; flagged if
            # it never settled, never silently a mid-transition snapshot (decision 0013).
            postfault = env.capture_settled_snapshot()

            # Information wall (§6): only the alert, built from the verified symptom,
            # crosses into the runtime. The fault and its spec stay on this side.
            alert = self.alerts.build_alert(verification)
            context = self._runtime_context(alert)
            raw_output, trajectory = self.agent.investigate(context)
            proposal = self.parser.parse(raw_output)

            features = shadow_scores = decision = execution = executed_action = None
            action = self._gate_eligible_action(proposal)
            if action is not None:
                features = self.extractor.extract(proposal, trajectory, context)
                shadow_scores = self.shadow_gates.score(features)
                decision = self.gate.decide(features)
                if self._should_execute(decision):
                    execution = self.executor.apply(action)
                    executed_action = self._executed_action(action, execution)

            # Verification, against the post-fault snapshot (§11.7).
            env.wait_stabilization_window()
            postaction = env.capture_snapshot()
            effective = self.effectiveness.verify(steady_state)
            harm = self.harm.check(executed_action, context, postfault.snapshot, postaction)
        except BaseException as exc:
            if not isinstance(exc, WorkloadGeneratorBusy):
                self._stop_load_after_failure(exc)
            raise

        # Measurements are complete. Everything from here on is cleanup, and no cleanup
        # failure may erase the run (§11.1; Milestone 3 third review). Order:
        #   1. clean up: stop the workload, reset, verify (failures become "not clean")
        #   2. determine reset_verified
        #   3. finalize the record
        #   4. only then recover a dirty environment (quarantine + rebuild)
        #   5. a failed recovery halts the worker, after the record exists
        cleanup_problems: list[str] = []
        try:
            self.workload.stop_workload()
        except Exception as exc:
            cleanup_problems.append(f"stop_workload failed: {type(exc).__name__}: {exc}")
        try:
            env.reset_environment()
            post_clean = env.verify_clean_state()
        except Exception as exc:  # any failure here (even an expired credential): keep the run
            post_clean = CleanStateResult(
                False, (f"post-run reset failed: {type(exc).__name__}: {exc}",)
            )
        if cleanup_problems:
            post_clean = CleanStateResult(False, tuple(cleanup_problems) + post_clean.problems)
        reset_verified = self._post_run_clean(post_clean)
        self.records.finalize(
            unit=unit,
            steady_state=steady_state,
            prefault=prefault,
            fault=fault,
            fault_verification=verification,
            postfault=postfault,
            alert=alert,
            trajectory=trajectory,
            proposal=proposal,
            features=features,
            shadow_scores=shadow_scores,
            decision=decision,
            execution=execution,
            postaction=postaction,
            effective=effective,
            harm=harm,
            reset_verified=reset_verified,
        )
        self._recover_if_dirty(post_clean)  # strictly after the record

    def _stop_load_after_failure(self, exc: BaseException) -> None:
        """Best-effort stop of the load this run started. A stop failure is attached to the
        original exception, never raised in its place."""
        try:
            self.workload.stop_workload()
        except Exception as stop_exc:
            exc.add_note(f"stop_workload after the failure also failed: {stop_exc}")

    # Runner hooks: points where the runner interprets a stage's result.

    def _require_pre_run_clean(self, clean_state) -> None:
        """If not clean: quarantine and rebuild, then discard the run (§5, §11.1).

        A rebuild that fails raises QuarantineFailed: the worker halts.
        """
        if clean_state.clean:
            return
        reason = "; ".join(clean_state.problems[:10])
        self.environment.quarantine_and_rebuild(reason=f"pre-run: {reason}")
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
        try:
            self.environment.quarantine_and_rebuild(
                reason="post-run: " + "; ".join(clean_state.problems[:10])
            )
        except Exception as exc:
            self._halt = (
                exc
                if isinstance(exc, QuarantineFailed)
                else QuarantineFailed(f"quarantine failed: {type(exc).__name__}: {exc}")
            )
            raise self._halt from exc


def build_runner() -> ExperimentRunner:
    """Wire the default components. Constructing them has no side effects."""
    workload = WorkloadGenerator()  # shared: the injector measures on the running workload
    return ExperimentRunner(
        environment=EnvironmentManager(),
        workload=workload,
        injector=FaultInjector(workload=workload),
        alerts=AlertGenerator(),
        agent=Agent(),
        parser=ProposalParser(),
        extractor=RiskFeatureExtractor(),
        shadow_gates=ShadowGateScorer(),
        gate=AlwaysActGate(),
        executor=ActionExecutor(),
        effectiveness=EffectivenessVerifier(),
        harm=HarmChecker(),
        records=RunRecordWriter(),
    )
