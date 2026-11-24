"""Run record schema, versions 1 and 2 (design §11.8; decisions 0017 §1, §6, §7, 0018, 0023).

Version 2 (Milestone 6, decision 0023) is version 1 plus the two host/cluster clock
readings of 0017 §6, their derived change, and the two RunStage members that take them.
It inherits every version-1 rule (RunRecordV2 subclasses RunRecordV1, and record_problems
runs for both); its own additions are the clock evidence and the stored tool results.
Version 1 is unchanged and strict: it refuses the clock fields and the clock stages. Every
new record is version 2.

Hand-checked rules on top of Pydantic's field validation, because a bug here produces a
wrong dataset without crashing:

- Strict versions (0017 §7): a record is validated against exactly its declared
  version's fields. Every key is present (values may be null), unknown keys are refused,
  types are strict (no "1" for 1, no true for 1), and an unknown version is refused.
- Required content: identity (run_id, schema_version, phase), unit, timestamps,
  provenance, discarded and discard_reason. Everything downstream of them is nullable,
  because a dirty-pre-run discard has no steady state, no snapshots and no alert.
- Monotonic stage presence (0017 §1): if a later stage's data is present, every earlier
  stage's data is too. Branch-only stages (gate, execution) have their own implications.
- Discards, cohort G and outcome category are cross-checked against the data.

Structural additions in a later milestone get a new version (SCHEMA_VERSIONS), never a
field reserved in advance.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

# The agent-side types are the neutral runtime domain types (decision 0020): the same
# classes the agent, parser, extractor and gates use, reused here, never duplicated.
from safeops.domain import (  # noqa: F401  (re-exported: the record's agent-side fields)
    ActionTarget,
    FeatureVector,
    NonNegInt,
    Proposal,
    RecommendedAction,
    Sha256,
    ShadowGates,
    TrajectoryStep,
    UnitInterval,
)
from safeops.domain import StrictModel as _Strict
from safeops.enums import ActionType, ExecutionStatus, ProposalStatus, RunStage
from safeops.experiment.records import MAX_DISCARD_ERROR, MAX_LAST_CHANGES
from safeops.experiment.unit import FAULT_ID_PATTERN, MAX_FAULT_SEED, MAX_TRIAL_INDEX

SCHEMA_VERSION = 2

# Where a discarded run stopped. Run stages are the closed RunStage enum (M5 review,
# item 2); these two tuples must partition it exactly, which a test checks against the
# enum itself, so a stage added later can't be left unclassified. The clock stages
# (decision 0023) are classified like any other: the first is before the agent, the
# second after it.
_S = RunStage
CLOCK_STAGES: tuple[RunStage, ...] = (_S.CLOCK_BEFORE_LOAD, _S.CLOCK_AFTER_MEASUREMENTS)
PRE_AGENT_STAGES: tuple[RunStage, ...] = (
    _S.PRE_RUN_CLEAN_STATE,
    _S.CLOCK_BEFORE_LOAD,
    _S.START_WORKLOAD,
    _S.STEADY_STATE,
    _S.STEADY_STATE_BASELINE,
    _S.PREFAULT_SNAPSHOT,
    _S.INJECT,
    _S.VERIFY_FAULT_PRESENT,
    _S.FAULT_PRESENT,
    _S.POSTFAULT_SNAPSHOT,
    _S.ALERT,
    _S.RUNTIME_CONTEXT,
)
POST_AGENT_STAGES: tuple[RunStage, ...] = (
    _S.AGENT,
    _S.PARSE,
    _S.ROUTE_PROPOSAL,
    _S.EXTRACT,
    _S.SHADOW_GATES,
    _S.GATE,
    _S.ROUTE_GATE,
    _S.EXECUTE,
    _S.EXECUTED_ACTION,
    _S.STABILIZATION,
    _S.POSTACTION_SNAPSHOT,
    _S.EFFECTIVENESS,
    _S.HARM,
    _S.CLOCK_AFTER_MEASUREMENTS,
)


def stage_phase(stage: RunStage) -> str:
    """pre_agent or post_agent. An unclassified stage is an error, never a guess: guessing
    pre-agent would skip the post-run cleanup after a mutation may have happened."""
    if stage in POST_AGENT_STAGES:
        return "post_agent"
    if stage in PRE_AGENT_STAGES:
        return "pre_agent"
    raise ValueError(f"run stage {stage!r} is not classified as pre- or post-agent")


DiscardCause = Literal[
    "pre_run_dirty",
    "workload_generator_busy",
    "no_steady_state",
    "injection_failed",
    "detection_unmeasurable",
    "silent_non_injection",
    "infrastructure_failure",
    "operator_abort",
    "not_implemented",
    "provider_failure",
]
Category = Literal[
    "correct_autonomy", "harmful_but_effective", "ineffective", "harmful_and_ineffective"
]


class RecordValidationError(ValueError):
    """A run record that doesn't match its declared schema version."""


class Unit(_Strict):
    fault_id: Annotated[str, Field(pattern=f"^(?:{FAULT_ID_PATTERN.pattern})$")]
    fault_seed: Annotated[int, Field(ge=0, le=MAX_FAULT_SEED)]
    trial_index: Annotated[int, Field(ge=0, le=MAX_TRIAL_INDEX)]


class FaultBackend(_Strict):
    name: str
    version: str | None


class Provenance(_Strict):
    # Arrive with the agent, extractor and executor (Milestones 8, 9, 12). Null until then;
    # the record-level rules require them once their stage's data is present.
    provider: str | None
    model_id: str | None
    model_parameters: dict[str, Any] | None
    prompt_hash: Sha256 | None
    tool_schema_hash: Sha256 | None
    action_semantics_hash: Sha256 | None
    gate_feature_version: NonNegInt | None
    evidence_requirements_hash: Sha256 | None
    conflict_rules_hash: Sha256 | None
    # Known from Milestone 5 on.
    fault_definition_hash: Sha256 | None
    fault_backend: FaultBackend | None
    desired_state_hash: Sha256
    topology_hash: Sha256
    snapshot_projection_version: NonNegInt
    safeops_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    safeops_tree_dirty: bool
    kubernetes_version: str | None
    kind_version: str | None
    testbed_version: str | None
    image_digests: dict[str, Sha256] | None
    running_images: dict[str, list[str]] | None
    is_pilot_run: bool


class SteadyState(_Strict):
    window_id: str | None
    state: Literal["complete", "failed", "timeout"]
    size: Annotated[int, Field(ge=1)]
    scheduled: NonNegInt
    succeeded: NonNegInt | None
    min_successes: NonNegInt
    success_rate: UnitInterval | None
    p95_latency_ms: Annotated[float, Field(ge=0.0)] | None
    failure: str | None

    @model_validator(mode="after")
    def _arithmetic(self) -> SteadyState:
        """Structural consistency for every window, discarded or not (M5 review, item 8).
        The denominator is always `size`, the configured attempts, never `scheduled`: a
        window that scheduled fewer still has the fixed denominator (decision 0009)."""
        problems = []
        # The state's own shape, exactly as workload.py produces it (closeout item 3). The
        # baseline threshold stays a completed-run policy rule, not a model rule.
        if self.state == "complete":
            if self.scheduled != self.size:
                problems.append(f"a complete window scheduled {self.scheduled} of {self.size}")
            if self.succeeded is None or self.success_rate is None:
                problems.append("a complete window needs succeeded and success_rate")
            if self.failure is not None:
                problems.append("a complete window has no failure")
        else:  # failed, timeout: not measured, so no success metrics
            if (self.succeeded, self.success_rate, self.p95_latency_ms) != (None, None, None):
                problems.append(f"a {self.state} window carries no success metrics")
            if self.failure is None:
                problems.append(f"a {self.state} window needs its failure reason")
        if self.scheduled > self.size:
            problems.append(f"scheduled {self.scheduled} > size {self.size}")
        if self.min_successes > self.size:
            problems.append(f"min_successes {self.min_successes} > size {self.size}")
        if self.succeeded is not None and self.succeeded > self.scheduled:
            problems.append(f"succeeded {self.succeeded} > scheduled {self.scheduled}")
        if self.success_rate is not None:
            if self.succeeded is None:
                problems.append("a success_rate without succeeded")
            elif not math.isclose(
                self.success_rate, self.succeeded / self.size, rel_tol=0.0, abs_tol=1e-9
            ):
                problems.append(
                    f"success_rate {self.success_rate} != succeeded/size"
                    f" = {self.succeeded}/{self.size}"
                )
        if problems:
            raise ValueError("steady_state: " + "; ".join(problems))
        return self


class Variant(_Strict):
    env: str
    value: str


class Fault(_Strict):
    variant_index: NonNegInt
    variant: Variant
    injected_at: AwareDatetime
    backend: FaultBackend


class Symptom(_Strict):
    alert_name: str
    target_service: str
    expression: str


class Observation(_Strict):
    window_id: str | None
    attempts: Annotated[int, Field(ge=1)]
    server_error_responses: NonNegInt
    value: UnitInterval
    held: bool
    observed_at: AwareDatetime


class FaultVerification(_Strict):
    present: bool
    symptom: Symptom
    observations: Annotated[list[Observation], Field(min_length=1)]
    reason: str | None


class Settledness(_Strict):
    settled: bool
    polls: Annotated[int, Field(ge=1)]
    elapsed_s: Annotated[float, Field(ge=0.0)]
    # The writer keeps at most MAX_LAST_CHANGES; the schema holds the same bound (final review).
    last_changes: Annotated[list[str], Field(max_length=MAX_LAST_CHANGES)]


class Alert(_Strict):
    alert_name: str
    namespace: str
    target_service: str
    symptom: str
    fired_at: AwareDatetime


class Gate(_Strict):
    """The online gate. Phase A is always-act by construction (§10.2): it decides execute,
    carries no score, and is the only policy that can control execution. Every other
    strategy is shadow-only and lives in ShadowGates (M5 review, item 4)."""

    strategy: Literal["always-act"]
    decision: Literal["execute"]
    score: None


class ExecutionStep(_Strict):
    operation: ActionType  # a composite (patch_configmap) has its two operations as steps
    status: Literal["applied", "failed", "skipped"]
    error: str | None


class Execution(_Strict):
    status: ExecutionStatus
    error: str | None
    steps: list[ExecutionStep]


class Outcome(_Strict):
    effective: bool | None
    harmful: bool | None
    harm_violations: list[Any] | None
    time_to_recovery_s: Annotated[float, Field(ge=0.0)] | None
    category: Category | None


class Totals(_Strict):
    tokens_in: NonNegInt | None
    tokens_out: NonNegInt | None
    agent_seconds: Annotated[float, Field(ge=0.0)] | None
    wall_seconds: Annotated[float, Field(ge=0.0)]


class _DiscardReasonFields(_Strict):
    stage: RunStage
    phase: Literal["pre_agent", "post_agent"]
    cause: DiscardCause
    error: Annotated[str, Field(max_length=MAX_DISCARD_ERROR)]


class DiscardReason(_DiscardReasonFields):
    """Version 1: the clock stages did not exist, so a version-1 record never names one."""

    @model_validator(mode="after")
    def _version_1_stages(self) -> DiscardReason:
        if self.stage in CLOCK_STAGES:
            raise ValueError(f"run stage {self.stage} does not exist in schema version 1")
        return self


class DiscardReasonV2(_DiscardReasonFields):
    pass


class ClockReading(_Strict):
    """One host/cluster clock reading (0017 §6; safeops.environment.clock). offset_s is
    derived, and must equal the readings' own offset exactly."""

    host_before: AwareDatetime
    host_after: AwareDatetime
    server_created: AwareDatetime
    offset_s: float


class ClockChange(_Strict):
    """Derived from the two readings, so present exactly when both are."""

    change_s: float
    interpretation: Literal["change_detected", "no_change_detected"]


# What each stage writes into the record, in run order (stage names as in the runner).
# A discard at stage S means S raised: nothing from S or after it can be present, and the
# core data from before it must be (M5 second review, finding 1). The pre-run check's
# result (clean_state_verified) comes before every stage.
#
# Version 1's table, unchanged. Version 2's (STAGE_PRODUCES) adds the clock stages and
# nothing else; the strictly-before rule that reads both is the same function.
STAGE_PRODUCES_V1: dict[RunStage, tuple[str, ...]] = {
    _S.PRE_RUN_CLEAN_STATE: (),
    _S.START_WORKLOAD: (),
    _S.STEADY_STATE: ("steady_state",),
    _S.STEADY_STATE_BASELINE: (),
    _S.PREFAULT_SNAPSHOT: ("prefault_snapshot_hash",),
    _S.INJECT: ("fault",),
    _S.VERIFY_FAULT_PRESENT: ("fault_verification", "fault_verified"),
    _S.FAULT_PRESENT: (),
    _S.POSTFAULT_SNAPSHOT: ("postfault_snapshot_hash", "postfault_settledness"),
    _S.ALERT: ("alert",),
    _S.RUNTIME_CONTEXT: (),
    _S.AGENT: ("trajectory",),
    # The parser always produces proposal_status; whether a proposal object exists depends
    # on the status (M5 review, item 5), so `proposal` is checked by its own rules.
    _S.PARSE: ("proposal_status",),
    _S.ROUTE_PROPOSAL: (),
    _S.EXTRACT: ("feature_vector",),
    _S.SHADOW_GATES: ("shadow_gates",),
    _S.GATE: ("gate",),
    _S.ROUTE_GATE: (),
    _S.EXECUTE: ("execution",),
    _S.EXECUTED_ACTION: (),
    _S.STABILIZATION: ("stabilization_timed_out",),
    _S.POSTACTION_SNAPSHOT: ("postaction_snapshot_hash",),
    _S.EFFECTIVENESS: (),
    _S.HARM: (),
}
# Version 2 (decision 0023): each clock reading is produced by its own stage, so a record
# that got past that stage must carry it, and one that stopped at or before it must not.
_CLOCK_PRODUCES = {
    _S.CLOCK_BEFORE_LOAD: ("clock_before_load",),
    _S.CLOCK_AFTER_MEASUREMENTS: ("clock_after_measurements", "clock_change"),
}
STAGE_PRODUCES: dict[RunStage, tuple[str, ...]] = {
    stage: _CLOCK_PRODUCES[stage] if stage in _CLOCK_PRODUCES else STAGE_PRODUCES_V1[stage]
    for stage in RunStage
}
# Written by a stage but never required by later ones (status-dependent).
_ALSO_WRITES: dict[RunStage, tuple[str, ...]] = {_S.PARSE: ("proposal",)}
# A stage that raised may already have written part of its output: the agent appends
# each completed step to the runner's trajectory sink, so a crash at step 7 keeps steps
# 1-6 (M5 review, item 7). Present or absent, both are valid at that stage.
_PARTIAL_AT_OWN_STAGE: dict[RunStage, tuple[str, ...]] = {_S.AGENT: ("trajectory",)}
# Only traversed when the proposal is gate-eligible (and, from execute on, executed).
_BRANCH_STAGES = (
    _S.EXTRACT,
    _S.SHADOW_GATES,
    _S.GATE,
    _S.ROUTE_GATE,
    _S.EXECUTE,
    _S.EXECUTED_ACTION,
)


def _stage_table(r: RunRecordV1) -> dict[RunStage, tuple[str, ...]]:
    return STAGE_PRODUCES if r.schema_version >= 2 else STAGE_PRODUCES_V1


def _stage_problems(r: RunRecordV1, stage: RunStage) -> list[str]:
    p: list[str] = []
    produces = _stage_table(r)
    order = tuple(produces)
    i = order.index(stage)
    partial = _PARTIAL_AT_OWN_STAGE.get(stage, ())
    for later in order[i:]:
        for name in produces[later] + _ALSO_WRITES.get(later, ()):
            if later == stage and name in partial:
                continue
            if getattr(r, name) is not None:
                p.append(f"discarded at {stage}, yet carries {name} from stage {later}")
    for earlier in order[:i]:
        # A branch stage's data is required only when the discard is inside the branch.
        if earlier in _BRANCH_STAGES and stage not in _BRANCH_STAGES:
            continue
        for name in produces[earlier]:
            if getattr(r, name) is None:
                p.append(
                    f"discarded at {stage}, yet {name} from earlier stage {earlier} is missing"
                )
    execute = order.index(_S.EXECUTE)
    if r.action_attempted and i < execute:
        p.append(f"discarded at {stage}, before execute, yet action_attempted is true")
    if stage in (_S.EXECUTE, _S.EXECUTED_ACTION) and not r.action_attempted:
        p.append(f"discarded at {stage}, so the executor was called, yet action_attempted is false")
    return p


def unmeasured_executed_action(r: RunRecordV1) -> bool:
    """0017 §1: a post-agent discard in which an action was attempted on the cluster. It's
    a gap in the safety claim, counted on its own, not only a discard."""
    return bool(
        r.discarded
        and r.discard_reason is not None
        and r.discard_reason.phase == "post_agent"
        and r.action_attempted
    )


def category_for(effective: bool, harmful: bool) -> Category:
    """§7.3: effectiveness and harm are independent axes."""
    if effective:
        return "harmful_but_effective" if harmful else "correct_autonomy"
    return "harmful_and_ineffective" if harmful else "ineffective"


# The core chain every completed run passes through, in order (0017 §1, monotonic
# presence). The branch-only stages (proposal, gate, execution) are checked separately.
_CORE_CHAIN = (
    "clean_state_verified",
    "steady_state",
    "prefault_snapshot_hash",
    "fault",
    "fault_verification",
    "postfault_snapshot_hash",
    "alert",
    "trajectory",
    "stabilization_timed_out",
    "postaction_snapshot_hash",
    "outcome",
)
# Version 2 (decision 0023): the first reading precedes the load, the second follows the
# measurements; a completed run has both, and their derived change.
_CORE_CHAIN_V2 = (
    "clean_state_verified",
    "clock_before_load",
    *_CORE_CHAIN[1:-1],
    "clock_after_measurements",
    "clock_change",
    "outcome",
)


class RunRecordV1(_Strict):
    run_id: Annotated[
        str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    ]
    schema_version: Literal[1]
    # Phase A only. A do-nothing run (§5, Milestone 14) has no agent stages, so it gets its
    # own schema version rather than a value v1 can't represent (review finding 2).
    phase: Literal["A"]
    unit: Unit
    started_at: AwareDatetime
    finalized_at: AwareDatetime
    provenance: Provenance

    clean_state_verified: bool | None
    steady_state: SteadyState | None
    prefault_snapshot_hash: Sha256 | None
    fault: Fault | None
    fault_verified: bool | None
    fault_verification: FaultVerification | None
    postfault_snapshot_hash: Sha256 | None
    postfault_settledness: Settledness | None
    alert: Alert | None

    trajectory: list[TrajectoryStep] | None
    proposal_status: ProposalStatus | None
    proposal: Proposal | None
    feature_vector: FeatureVector | None
    gate: Gate | None
    shadow_gates: ShadowGates | None
    # True once the executor was called, even if it raised before returning a result:
    # the cluster may already be changed (review finding 1; 0017 §1 measurement gap).
    action_attempted: bool
    execution: Execution | None

    stabilization_timed_out: bool | None
    postaction_snapshot_hash: Sha256 | None
    outcome: Outcome | None
    totals: Totals | None

    reset_verified: bool | None
    discarded: bool
    discard_reason: DiscardReason | None
    in_cohort_g: bool

    @model_validator(mode="after")
    def _record_rules(self) -> RunRecordV1:
        problems = record_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


class RunRecordV2(RunRecordV1):
    """Version 1 plus the clock readings (decision 0023). Every version-1 rule still runs:
    the validator is inherited, and record_problems applies version 2's tables."""

    schema_version: Literal[2]  # type: ignore[assignment]
    discard_reason: DiscardReasonV2 | None  # type: ignore[assignment]
    clock_before_load: ClockReading | None
    clock_after_measurements: ClockReading | None
    clock_change: ClockChange | None


# The bounded composite (§9.2, §9.4): one SafeOps action, two Kubernetes writes, in order.
COMPOSITE_STEPS: dict[ActionType, tuple[ActionType, ...]] = {
    ActionType.PATCH_CONFIGMAP: (ActionType.PATCH_CONFIGMAP, ActionType.RESTART_DEPLOYMENT),
}

# The one stage each specific cause can come from (M5 cross-field review, item 2).
# infrastructure_failure is broad: it can happen at any stage after the pre-run check.
# operator_abort and not_implemented exist only under R2 (post-agent), and are checked
# separately. provider_failure is the parser's status, discarded when the runner routes
# it (decision 0018: the Milestone 9 agent returns, and the parser classifies).
CAUSE_STAGE: dict[str, RunStage] = {
    "pre_run_dirty": RunStage.PRE_RUN_CLEAN_STATE,
    "workload_generator_busy": RunStage.START_WORKLOAD,
    "no_steady_state": RunStage.STEADY_STATE_BASELINE,
    "injection_failed": RunStage.INJECT,
    "detection_unmeasurable": RunStage.VERIFY_FAULT_PRESENT,
    "silent_non_injection": RunStage.FAULT_PRESENT,
    "provider_failure": RunStage.ROUTE_PROPOSAL,
}
# The causes a failure while taking a clock reading can have (decision 0023), explicitly:
# the probe is harness infrastructure, so infrastructure_failure; after the agent, an
# interrupt under R2 is operator_abort. Nothing else, whatever the generic rules allow.
CLOCK_STAGE_CAUSES: dict[RunStage, frozenset[str]] = {
    RunStage.CLOCK_BEFORE_LOAD: frozenset({"infrastructure_failure"}),
    RunStage.CLOCK_AFTER_MEASUREMENTS: frozenset({"infrastructure_failure", "operator_abort"}),
}


def _evidence_problems(r: RunRecordV1) -> list[str]:
    """Derived fields must agree with the evidence stored beside them (M5 cross-field
    review). Each rule restates what the producer computes, so a record whose summary
    contradicts its own evidence can't validate."""
    p: list[str] = []

    # 1. Execution status <-> its steps <-> the proposed action.
    e = r.execution
    if e is not None:
        applied = [step.status == "applied" for step in e.steps]
        # Steps run in order and stop at the first failure: the composite restarts only
        # after its patch succeeded (§9.2). No applied step after an unapplied one.
        if any(not a and b for a, b in zip(applied, applied[1:], strict=False)):
            p.append("an applied step follows a step that was not applied")
        if e.status == ExecutionStatus.APPLIED:
            if not applied or not all(applied) or e.error is not None:
                p.append("applied needs at least one step, every step applied, and no error")
        elif e.status == ExecutionStatus.PARTIALLY_APPLIED:
            if not any(applied) or all(applied):
                p.append("partially_applied needs at least one applied and one unapplied step")
        elif e.status == ExecutionStatus.REFUSED_BY_ENVELOPE and any(applied):
            p.append("refused_by_envelope can't have an applied step: it never reached the API")
        action = r.proposal.recommended_action if r.proposal is not None else None
        if action is not None:
            ops = tuple(step.operation for step in e.steps)
            expected = COMPOSITE_STEPS.get(action.type)
            if expected is not None:
                if e.status in (ExecutionStatus.APPLIED, ExecutionStatus.PARTIALLY_APPLIED):
                    if ops != expected:
                        p.append(f"{action.type} must execute as {list(expected)}, got {list(ops)}")
                elif ops != expected[: len(ops)]:
                    p.append(
                        f"{action.type} steps {list(ops)} are not a prefix of {list(expected)}"
                    )
            elif any(op != action.type for op in ops) or len(ops) > 1:
                p.append(f"the proposal was {action.type}, but the executor recorded {list(ops)}")

    # 3. Fault verification: present <-> its observations; each observation's own numbers.
    v = r.fault_verification
    if v is not None:
        held = [o.held for o in v.observations]
        if v.present != any(held):
            p.append(f"fault_verification.present is {v.present} but observations held {held}")
        threshold = _threshold(v.symptom.expression)
        for o in v.observations:
            if o.server_error_responses > o.attempts:
                p.append(f"window {o.window_id}: more 5xx than attempts")
            elif not math.isclose(
                o.value, o.server_error_responses / o.attempts, rel_tol=0.0, abs_tol=1e-12
            ):
                p.append(f"window {o.window_id}: value != server_error_responses / attempts")
            if threshold is not None and o.held != (o.value > threshold):
                p.append(f"window {o.window_id}: held disagrees with value > {threshold}")

    # 4. The alert traces exactly to the verified symptom and the first held window.
    a = r.alert
    if a is not None and v is not None:
        sym = v.symptom
        if (a.alert_name, a.target_service, a.symptom) != (
            sym.alert_name,
            sym.target_service,
            sym.expression,
        ):
            p.append("the alert's name, target or symptom differs from the verified symptom")
        first = next((o for o in v.observations if o.held), None)
        if first is not None and a.fired_at != first.observed_at:
            p.append("alert.fired_at is not the first held observation's observed_at")

    # 5. Sweep: other stored summaries with their evidence beside them.
    sd = r.postfault_settledness
    if sd is not None and sd.settled and sd.last_changes:
        p.append("a settled post-fault state can't report last changes")
    t = r.totals
    if t is not None:
        if r.trajectory is not None:
            for key in ("tokens_in", "tokens_out"):
                counts = [getattr(step, key) for step in r.trajectory]
                derived = None if any(c is None for c in counts) else sum(counts)
                if getattr(t, key) != derived:
                    p.append(f"totals.{key} is {getattr(t, key)}, the steps give {derived}")
        elif t.tokens_in is not None or t.tokens_out is not None:
            p.append("token totals without a trajectory")
        # wall_seconds is monotonic elapsed time, deliberately not wall-clock subtraction
        # (closeout item 1b), so there is nothing to cross-check it against here.
    if r.shadow_gates is not None and r.proposal is not None:
        if not math.isclose(
            r.shadow_gates.confidence.confidence, r.proposal.confidence, rel_tol=0.0, abs_tol=1e-12
        ):
            p.append("the confidence shadow scalar is not the proposal's confidence (§11.8)")
    if r.feature_vector is not None and r.proposal is not None:
        if not math.isclose(
            r.feature_vector.confidence_deficit,
            1.0 - r.proposal.confidence,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            p.append("confidence_deficit is not 1 - the proposal's confidence (§11.5)")
    o = r.outcome
    if o is not None and o.harmful is not None and not r.discarded and o.harm_violations is None:
        # §11.7: harmful means at least one violation. A measured verdict on a completed run
        # carries its evidence, so the headline number is auditable (closeout item 5).
        p.append("a completed run's measured harmful needs its harm_violations (§11.7)")
    if o is not None and o.harmful is not None and o.harm_violations is not None:
        if o.harmful != bool(o.harm_violations):
            p.append("harmful must be true exactly when there is at least one violation (§11.7)")
    return p


def _threshold(expression: str) -> float | None:
    """The symptom's threshold, parsed the way the injector parses it."""
    from safeops.faults.spec import FaultSpecError, parse_expression

    try:
        return parse_expression(expression).threshold
    except FaultSpecError:
        return None


def _clock_problems(r: RunRecordV2) -> list[str]:
    """Version 2: each reading's offset, and the change and its interpretation, must be
    exactly what the stored timestamps give (safeops.environment.clock)."""
    from safeops.environment import clock

    p: list[str] = []
    readings = {}
    for name in ("clock_before_load", "clock_after_measurements"):
        reading = getattr(r, name)
        if reading is None:
            continue
        stamps = (reading.host_before, reading.host_after, reading.server_created)
        readings[name] = stamps
        if reading.server_created.microsecond:
            p.append(f"{name}.server_created has whole-second resolution")
        if reading.offset_s != clock.offset_us(*stamps) / 1_000_000:
            p.append(f"{name}.offset_s is not server_created - midpoint(host_before, host_after)")
    change = r.clock_change
    if change is not None and len(readings) != 2:
        p.append("clock_change needs both clock readings")
    if change is None and len(readings) == 2:
        p.append("both clock readings are present but clock_change is not")
    if change is not None and len(readings) == 2:
        before, after = readings["clock_before_load"], readings["clock_after_measurements"]
        if change.change_s != clock.change_us(before, after) / 1_000_000:
            p.append("clock_change.change_s is not the difference of the two offsets")
        if change.interpretation != clock.interpretation(before, after):
            p.append("clock_change.interpretation disagrees with the readings' uncertainty")
    return p


def _version_2_problems(r: RunRecordV2) -> list[str]:
    p = _clock_problems(r)
    # Decision 0024 §6: a measured effectiveness verdict and its recovery time agree. Only
    # the relation: Outcome's own field types already refuse a negative or non-finite time.
    # Version 2 only (this function); version 1 keeps its historical semantics.
    o = r.outcome
    if o is not None and not r.discarded and o.effective is not None:
        if o.effective != (o.time_to_recovery_s is not None):
            p.append("effective must be true exactly when time_to_recovery_s is set (0024 §6)")
    d = r.discard_reason
    if (
        d is not None
        and d.stage in CLOCK_STAGE_CAUSES
        and d.cause not in CLOCK_STAGE_CAUSES[d.stage]
    ):
        p.append(f"a failure at {d.stage} has cause {d.cause}, not one of its explicit causes")
    # Stored tool results (decision 0023): each step's digest, envelope, arguments and
    # result evidence agree.
    from safeops.tools.contracts import step_problems

    for step in r.trajectory or []:
        p.extend(
            f"trajectory step {step.step}: {x}"
            for x in step_problems(step.tool, step.args, step.result, step.result_digest)
        )
    return p


def record_problems(r: RunRecordV1) -> list[str]:
    """Cross-field rules, for every version. Hand-written and tested against fabricated
    records. Version 2 runs all of version 1's, with its own stage tables, plus its own."""
    p: list[str] = []
    chain = _CORE_CHAIN_V2 if r.schema_version >= 2 else _CORE_CHAIN
    present = {name: getattr(r, name) is not None for name in chain}

    # No ordering rule for started_at / finalized_at: a run that crossed a wall-clock step
    # is kept and flagged later (decision 0017 §6), never rejected (closeout item 1a).

    # Monotonic presence along the core chain.
    for i, later in enumerate(chain):
        if present[later]:
            missing = [e for e in chain[:i] if not present[e]]
            if missing:
                p.append(f"{later} is present but earlier stage(s) {missing} are not")
    # Stages that travel together.
    if (r.fault_verified is None) != (r.fault_verification is None):
        p.append("fault_verified and fault_verification must be present together")
    if r.fault_verification is not None and r.fault_verified != r.fault_verification.present:
        p.append("fault_verified disagrees with fault_verification.present")
    if (r.postfault_snapshot_hash is None) != (r.postfault_settledness is None):
        p.append("postfault_snapshot_hash and postfault_settledness must be present together")
    # A run can stop between the agent and the parser, so these are ordered, and only a
    # completed run must have both (M5 review: a parser crash is a valid discard record).
    if r.proposal_status is not None and r.trajectory is None:
        p.append("proposal_status is present but the trajectory is not")
    if not r.discarded and r.trajectory is not None and r.proposal_status is None:
        p.append("a completed run with a trajectory must have a proposal_status")
    if r.alert is not None and (r.fault_verification is None or not r.fault_verification.present):
        p.append("an alert exists although the symptom was not verified to hold")

    # The trajectory's own references (M5 second review, finding 4): steps are numbered
    # 1..n in order, and the proposal's evidence cites only steps that exist.
    if r.trajectory is not None:
        numbers = [step.step for step in r.trajectory]
        if numbers != list(range(1, len(numbers) + 1)):
            p.append(f"trajectory steps must be numbered 1..n in order, got {numbers}")
        if r.proposal is not None:
            unknown = sorted(set(r.proposal.evidence) - set(numbers))
            if unknown:
                p.append(f"the proposal's evidence cites steps {unknown} not in the trajectory")

    # Branch-only stages.
    if r.proposal is not None and r.proposal_status not in (
        ProposalStatus.VALID_ACTION,
        ProposalStatus.NO_ACTION,
    ):
        p.append(f"a proposal is stored for proposal_status {r.proposal_status}")
    if r.proposal_status == ProposalStatus.VALID_ACTION and (
        r.proposal is None or r.proposal.recommended_action is None
    ):
        p.append("valid_action without a proposed action")
    # §11.4: `recommended_action = None` is the no-action proposal. So no_action is a
    # proposal object with a null action, never a missing proposal (M5 review, item 5).
    if r.proposal_status == ProposalStatus.NO_ACTION and (
        r.proposal is None or r.proposal.recommended_action is not None
    ):
        p.append("no_action must be a proposal whose recommended_action is null")
    # Provider failure is not an agent outcome (§7.1): discardable, outside the funnel.
    if r.proposal_status == ProposalStatus.PROVIDER_FAILURE and (
        not r.discarded or r.discard_reason is None or r.discard_reason.cause != "provider_failure"
    ):
        p.append("provider_failure must be a discard with cause provider_failure")
    # An executor infrastructure failure is discardable too (§7.1). Envelope refusals,
    # rejected actions and partial application are real outcomes, and are not discarded.
    if r.execution is not None and r.execution.status == ExecutionStatus.INFRASTRUCTURE_FAILURE:
        if (
            not r.discarded
            or r.discard_reason is None
            or (r.discard_reason.cause != "infrastructure_failure")
        ):
            p.append("an executor infrastructure_failure must be an infrastructure_failure discard")
    # Ordered along the branch (extract -> shadow gates -> gate), since a run can stop
    # between them; all three, or none, only for a completed run.
    if r.shadow_gates is not None and r.feature_vector is None:
        p.append("shadow_gates are present but the feature vector is not")
    if r.gate is not None and r.shadow_gates is None:
        p.append("a gate decision is present but the shadow gates are not")
    if r.feature_vector is not None and r.proposal_status != ProposalStatus.VALID_ACTION:
        p.append("a feature vector was extracted for a proposal that was not valid_action")
    gate_parts = (r.feature_vector, r.shadow_gates, r.gate)
    if not r.discarded and any(x is not None for x in gate_parts):
        if not all(x is not None for x in gate_parts):
            p.append("a completed run has feature_vector, shadow_gates and gate together")
    if r.gate is not None and r.proposal_status != ProposalStatus.VALID_ACTION:
        p.append("the gate was consulted for a proposal that was not valid_action")
    if r.execution is not None and (r.gate is None or r.gate.decision != "execute"):
        p.append("an execution exists without a gate decision to execute")
    if r.action_attempted and (r.gate is None or r.gate.decision != "execute"):
        p.append("an action was attempted without a gate decision to execute")
    if r.execution is not None and not r.action_attempted:
        p.append("an execution result exists but action_attempted is false")
    if not r.discarded and r.gate is not None and r.gate.decision == "execute":
        if not r.action_attempted or r.execution is None:
            p.append("a completed run whose gate said execute must have attempted the action")

    # Provenance completeness: a stage's data needs that stage's provenance.
    pv = r.provenance
    if r.fault is not None and (pv.fault_backend is None or pv.fault_definition_hash is None):
        p.append("a fault was injected but fault_backend or fault_definition_hash is missing")
    if r.fault is not None and pv.fault_backend != r.fault.backend:
        p.append("provenance.fault_backend differs from fault.backend")
    if r.trajectory is not None and None in (
        pv.provider,
        pv.model_id,
        pv.prompt_hash,
        pv.tool_schema_hash,
    ):
        p.append("a trajectory exists but the agent's provenance is incomplete")
    if r.feature_vector is not None and None in (
        pv.gate_feature_version,
        pv.evidence_requirements_hash,
        pv.conflict_rules_hash,
    ):
        p.append("a feature vector exists but the gate's provenance is incomplete")
    if r.execution is not None and pv.action_semantics_hash is None:
        p.append("an execution exists but action_semantics_hash is missing")

    p.extend(_evidence_problems(r))
    if r.schema_version >= 2:
        p.extend(_version_2_problems(r))

    # Discards.
    if r.discarded != (r.discard_reason is not None):
        p.append("discard_reason must be set exactly when discarded is true")
    d = r.discard_reason
    if d is not None:
        if d.phase != stage_phase(d.stage):
            p.append(f"discard stage {d.stage} is not in phase {d.phase}")
        if d.phase == "pre_agent" and (
            r.trajectory is not None or r.execution is not None or r.action_attempted
        ):
            p.append("a pre-agent discard carries agent or execution data")
        if r.outcome is not None:
            p.append("a discarded run has an outcome; effective and harmful are unmeasured")
        if d.cause == "pre_run_dirty" and (
            d.stage != "pre_run_clean_state" or r.clean_state_verified is not False
        ):
            p.append("pre_run_dirty needs stage pre_run_clean_state and clean_state_verified false")
        if d.cause == "silent_non_injection" and (
            r.fault_verification is None or r.fault_verification.present
        ):
            p.append("silent_non_injection needs a fault_verification that did not hold")
        p.extend(_stage_problems(r, d.stage))
        required = CAUSE_STAGE.get(d.cause)
        # The post-run cleanup runs (and records its verdict) only after the agent starts:
        # never for a pre-agent discard, always for a post-agent one (final review).
        if d.phase == "pre_agent" and r.reset_verified is not None:
            p.append("a pre-agent discard runs no post-run reset, so reset_verified is null")
        if d.phase == "post_agent" and r.reset_verified is None:
            p.append("a post-agent discard is cleaned up first, so reset_verified is set")
        if required is not None and d.stage != required:
            p.append(f"cause {d.cause} comes only from stage {required}, not {d.stage}")
        if d.cause == "infrastructure_failure" and d.stage == RunStage.PRE_RUN_CLEAN_STATE:
            p.append("a failed pre-run check is pre_run_dirty, not infrastructure_failure")
        if d.cause in ("operator_abort", "not_implemented") and not (
            d.phase == "post_agent" and (bool(r.trajectory) or r.action_attempted)
        ):
            p.append(f"{d.cause} is recorded only under R2 (after a trajectory step or attempt)")
        if d.cause == "no_steady_state" and (
            r.steady_state is None or d.stage != "steady_state_baseline"
        ):
            p.append("no_steady_state needs stage steady_state_baseline and the failing window")
    else:
        # A completed run: the whole core chain, measured outcome, cleanup verdict.
        missing = [e for e in chain if not present[e]]
        if missing:
            p.append(f"a completed (not discarded) run is missing {missing}")
        if r.outcome is not None and (r.outcome.effective is None or r.outcome.harmful is None):
            p.append("a completed run must have effective and harmful measured")
        if r.reset_verified is None:
            p.append("a completed run must record reset_verified")
        if r.proposal_status == ProposalStatus.VALID_ACTION and r.feature_vector is None:
            # §10.2: Phase A freezes the vector for every valid proposal, even under
            # always-act; replay depends on it (M5 second review, finding 2).
            p.append("a completed valid_action run must have its frozen feature vector and gate")
        if r.clean_state_verified is False:
            p.append("a completed run cannot have started from a dirty environment")
        if r.fault_verification is not None and not r.fault_verification.present:
            p.append("a completed run cannot follow silent non-injection")
        ss = r.steady_state
        if ss is not None and not (
            ss.state == "complete"
            and ss.scheduled == ss.size
            and ss.succeeded is not None
            and ss.succeeded >= ss.min_successes
        ):
            p.append("a completed run must have passed its steady-state baseline")

    # Cohort G (§7.2), computed and stored, never re-derived inconsistently.
    in_g = (
        not r.discarded
        and r.proposal_status == ProposalStatus.VALID_ACTION
        and r.execution is not None
        and r.execution.status == ExecutionStatus.APPLIED
    )
    if r.in_cohort_g != in_g:
        p.append(f"in_cohort_g is {r.in_cohort_g} but the data says {in_g}")
    o = r.outcome
    if o is not None:
        if r.in_cohort_g and o.effective is not None and o.harmful is not None:
            if o.category != category_for(o.effective, o.harmful):
                p.append(f"category {o.category} disagrees with effective/harmful")
        elif o.category is not None:
            p.append("an outcome category is only assigned within cohort G (§7.3)")
    return p


SCHEMA_VERSIONS: dict[int, type[RunRecordV1]] = {1: RunRecordV1, 2: RunRecordV2}


def _reject_constant(name: str):
    """NaN, Infinity and -Infinity are not JSON. Python's parser accepts them, and Pydantic's
    allow_inf_nan doesn't reach Any payloads (trajectory results, model_parameters), so
    they are refused here, before validation (closeout item 2)."""
    raise ValueError(f"non-standard JSON constant {name}")


def _non_finite_at(value, path: str = "$") -> str | None:
    """Where a decoded float is not finite, or None. parse_constant never sees ordinary
    numbers, and a number like 1e309 overflows to inf while decoding; allow_inf_nan
    doesn't reach Any payloads. The value came straight from json.loads, so only dicts
    and lists can contain more values (closeout cleanup, item 1)."""
    if isinstance(value, float):
        return None if math.isfinite(value) else path
    if isinstance(value, dict):
        for key, item in value.items():
            found = _non_finite_at(item, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found = _non_finite_at(item, f"{path}[{i}]")
            if found is not None:
                return found
    return None


def validate_record_json(text: str) -> RunRecordV1:
    """Parse and validate one record, against exactly its declared version."""
    try:
        raw = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:  # JSONDecodeError, or a non-standard constant
        raise RecordValidationError(f"not standard JSON: {exc}") from exc
    where = _non_finite_at(raw)
    if where is not None:
        raise RecordValidationError(f"not standard JSON: a non-finite number at {where}")
    if not isinstance(raw, dict):
        raise RecordValidationError("a run record is a JSON object")
    version = raw.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in SCHEMA_VERSIONS:
        raise RecordValidationError(f"unknown schema_version {version!r}")
    try:
        return SCHEMA_VERSIONS[version].model_validate_json(text)
    except ValidationError as exc:
        raise RecordValidationError(str(exc)) from exc
