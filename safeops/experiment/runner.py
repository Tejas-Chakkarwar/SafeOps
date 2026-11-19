"""Experiment runner: one run of the §5 sequence for one experimental unit.

Each step calls the component that owns it. Wherever the runner itself has to interpret
a result (discard on a dirty cluster, route on proposal_status, and so on), it calls one
of its own stubbed hooks. Until that hook is implemented, an implemented stage cannot be
silently ignored: the run stops loudly at the hook instead.

Keep this call order in step with ``safeops.experiment.sequence.RUN_SEQUENCE``.
tests/test_run_sequence.py enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass

from safeops.agent.loop import Agent
from safeops.agent.parser import ProposalParser
from safeops.cluster import CommandError
from safeops.environment.manager import CleanStateResult, EnvironmentManager, QuarantineFailed
from safeops.environment.workload import WorkloadGenerator, check_steady_state
from safeops.executor.executor import ActionExecutor
from safeops.experiment.records import RunRecordWriter
from safeops.faults.alerts import AlertGenerator
from safeops.faults.injector import FaultInjector
from safeops.features.extractor import RiskFeatureExtractor
from safeops.gate.always_act import AlwaysActGate
from safeops.gate.shadow import ShadowGateScorer
from safeops.stubs import stub
from safeops.verify.effectiveness import EffectivenessVerifier
from safeops.verify.harm import HarmChecker


class PreRunDiscard(Exception):
    """The environment wasn't clean before the run. The run is discarded and counted
    (§11.1), and the worker was quarantined and rebuilt."""


@dataclass(frozen=True)
class RunUnit:
    """The experimental unit (§10.1)."""

    fault_id: str
    fault_seed: int
    trial_index: int


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

    def run(self, unit: RunUnit) -> None:
        env = self.environment

        # Pre-run. A failure here is discardable (§7.1, §11.1).
        env.reset_environment()
        self._require_pre_run_clean(env.verify_clean_state())
        self.workload.start_workload()
        steady_state = self.workload.verify_steady_state()
        self._require_steady_state(steady_state)
        prefault = env.capture_snapshot()
        fault = self.injector.inject(unit.fault_id, unit.fault_seed)
        self._require_fault_present(self.injector.verify_fault_present(fault))
        postfault = env.capture_snapshot()  # harm baseline (§5, Rule 2)

        # Information wall (§6): only the symptom-built alert crosses into the runtime.
        alert = self.alerts.build_alert(fault)
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
        harm = self.harm.check(executed_action, context, postfault, postaction)
        self.workload.stop_workload()

        # Post-run. A completed run is kept even if this reset fails (§11.1).
        try:
            env.reset_environment()
            post_clean = env.verify_clean_state()
        except CommandError as exc:  # infrastructure failure during reset: keep the run
            post_clean = CleanStateResult(False, (f"post-run reset failed: {exc}",))
        reset_verified = self._post_run_clean(post_clean)
        self.records.finalize(
            unit=unit,
            steady_state=steady_state,
            prefault=prefault,
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
        if self._halt is not None:  # the record is written first; then the worker halts
            raise self._halt

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

    @stub(milestone=4)
    def _require_fault_present(self, present: bool) -> None:
        """Silent non-injection: discard before the agent starts (§11.2)."""

    @stub(milestone=4)
    def _runtime_context(self, alert):
        """Build IncidentRuntimeContext(alert, topology, now). It holds no fault spec (§6)."""

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
        """Return reset_verified. On failure, keep the run and quarantine (§11.1).

        A completed run is never discarded for a failed cleanup: the hardest runs to clean
        up are the ones where the agent did the most damage. If the rebuild also fails,
        the run's record is still written; then the worker halts.
        """
        if clean_state.clean:
            return True
        try:
            self.environment.quarantine_and_rebuild(
                reason="post-run: " + "; ".join(clean_state.problems[:10])
            )
        except QuarantineFailed as exc:
            self._halt = exc
        return False


def build_runner() -> ExperimentRunner:
    """Wire the default components. Constructing them has no side effects."""
    return ExperimentRunner(
        environment=EnvironmentManager(),
        workload=WorkloadGenerator(),
        injector=FaultInjector(),
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
