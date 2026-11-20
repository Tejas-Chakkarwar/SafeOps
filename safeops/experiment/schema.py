"""Run record schema, version 1 (design §11.8; decisions 0017 §1, §7 and 0018).

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

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from safeops.enums import ActionType, ExecutionStatus, ProposalStatus, RunStage
from safeops.experiment.unit import FAULT_ID_PATTERN, MAX_FAULT_SEED, MAX_TRIAL_INDEX

SCHEMA_VERSION = 1

Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegInt = Annotated[int, Field(ge=0)]

# Where a discarded run stopped. Run stages are the closed RunStage enum (M5 review,
# item 2); these two tuples must partition it exactly, which a test checks against the
# enum itself, so a stage added later can't be left unclassified.
_S = RunStage
PRE_AGENT_STAGES: tuple[RunStage, ...] = (
    _S.PRE_RUN_CLEAN_STATE,
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


class _Strict(BaseModel):
    # allow_inf_nan=False: NaN and infinities are refused everywhere (review finding 6).
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


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
    last_changes: list[str]


class Alert(_Strict):
    alert_name: str
    namespace: str
    target_service: str
    symptom: str
    fired_at: AwareDatetime


class TrajectoryStep(_Strict):
    step: Annotated[int, Field(ge=1)]
    tool: str
    args: dict[str, Any]
    result: Any
    result_digest: Sha256
    tokens_in: NonNegInt | None
    tokens_out: NonNegInt | None
    latency_ms: Annotated[float, Field(ge=0.0)] | None


class ActionTarget(_Strict):
    kind: str
    ns: str
    name: str


class RecommendedAction(_Strict):
    type: ActionType
    target: ActionTarget
    params: dict[str, Any]


class Proposal(_Strict):
    diagnosis: str
    recommended_action: RecommendedAction | None
    confidence: UnitInterval
    evidence: list[NonNegInt]
    prerequisites_claimed: list[str]


class FeatureVector(_Strict):
    irreversibility: UnitInterval
    blast_radius: UnitInterval
    persistence_risk: UnitInterval
    scope_outside_target: UnitInterval
    confidence_deficit: UnitInterval
    evidence_insufficiency: UnitInterval
    prerequisites_unverified: UnitInterval
    telemetry_conflict: UnitInterval


class Gate(_Strict):
    """The online gate. Phase A is always-act by construction (§10.2): it decides execute,
    carries no score, and is the only policy that can control execution. Every other
    strategy is shadow-only and lives in ShadowGates (M5 review, item 4)."""

    strategy: Literal["always-act"]
    decision: Literal["execute"]
    score: None


class _RiskScore(_Strict):
    risk_score: UnitInterval


class _Confidence(_Strict):
    confidence: UnitInterval


class _Draw(_Strict):
    draw: UnitInterval


class ShadowGates(_Strict):
    safeops: _RiskScore
    confidence: _Confidence
    random: _Draw


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


class DiscardReason(_Strict):
    stage: RunStage
    phase: Literal["pre_agent", "post_agent"]
    cause: DiscardCause
    error: str


# What each stage writes into the record, in run order (stage names as in the runner).
# A discard at stage S means S raised: nothing from S or after it can be present, and the
# core data from before it must be (M5 second review, finding 1). The pre-run check's
# result (clean_state_verified) comes before every stage.
STAGE_PRODUCES: dict[RunStage, tuple[str, ...]] = {
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
# Written by a stage but never required by later ones (status-dependent).
_ALSO_WRITES: dict[RunStage, tuple[str, ...]] = {_S.PARSE: ("proposal",)}
# A stage that raised may already have written part of its output: the agent appends
# each completed step to the runner's trajectory sink, so a crash at step 7 keeps steps
# 1-6 (M5 review, item 7). Present or absent, both are valid at that stage.
_PARTIAL_AT_OWN_STAGE: dict[RunStage, tuple[str, ...]] = {_S.AGENT: ("trajectory",)}
_STAGE_ORDER = tuple(STAGE_PRODUCES)
# Only traversed when the proposal is gate-eligible (and, from execute on, executed).
_BRANCH_STAGES = (
    _S.EXTRACT,
    _S.SHADOW_GATES,
    _S.GATE,
    _S.ROUTE_GATE,
    _S.EXECUTE,
    _S.EXECUTED_ACTION,
)


def _stage_problems(r: RunRecordV1, stage: RunStage) -> list[str]:
    p: list[str] = []
    i = _STAGE_ORDER.index(stage)
    partial = _PARTIAL_AT_OWN_STAGE.get(stage, ())
    for later in _STAGE_ORDER[i:]:
        for name in STAGE_PRODUCES[later] + _ALSO_WRITES.get(later, ()):
            if later == stage and name in partial:
                continue
            if getattr(r, name) is not None:
                p.append(f"discarded at {stage}, yet carries {name} from stage {later}")
    for earlier in _STAGE_ORDER[:i]:
        # A branch stage's data is required only when the discard is inside the branch.
        if earlier in _BRANCH_STAGES and stage not in _BRANCH_STAGES:
            continue
        for name in STAGE_PRODUCES[earlier]:
            if getattr(r, name) is None:
                p.append(
                    f"discarded at {stage}, yet {name} from earlier stage {earlier} is missing"
                )
    execute = _STAGE_ORDER.index(_S.EXECUTE)
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


def _evidence_problems(r: RunRecordV1) -> list[str]:
    """Derived fields must agree with the evidence stored beside them (M5 cross-field
    review). Each rule restates what the producer computes, so a record whose summary
    contradicts its own evidence can't validate."""
    p: list[str] = []

    # 1. Execution status <-> its steps <-> the proposed action.
    e = r.execution
    if e is not None:
        applied = [step.status == "applied" for step in e.steps]
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
        span = (r.finalized_at - r.started_at).total_seconds()
        if not math.isclose(t.wall_seconds, max(0.0, span), rel_tol=0.0, abs_tol=1e-6):
            p.append(f"totals.wall_seconds {t.wall_seconds} != finalized_at - started_at")
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


def record_problems(r: RunRecordV1) -> list[str]:
    """Cross-field rules. Hand-written and tested against fabricated records."""
    p: list[str] = []
    present = {name: getattr(r, name) is not None for name in _CORE_CHAIN}

    if r.finalized_at < r.started_at:
        p.append("finalized_at is before started_at")

    # Monotonic presence along the core chain.
    for i, later in enumerate(_CORE_CHAIN):
        if present[later]:
            missing = [e for e in _CORE_CHAIN[:i] if not present[e]]
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
        missing = [e for e in _CORE_CHAIN if not present[e]]
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


SCHEMA_VERSIONS: dict[int, type[RunRecordV1]] = {1: RunRecordV1}


def validate_record_json(text: str) -> RunRecordV1:
    """Parse and validate one record, against exactly its declared version."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordValidationError(f"not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RecordValidationError("a run record is a JSON object")
    version = raw.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in SCHEMA_VERSIONS:
        raise RecordValidationError(f"unknown schema_version {version!r}")
    try:
        return SCHEMA_VERSIONS[version].model_validate_json(text)
    except ValidationError as exc:
        raise RecordValidationError(str(exc)) from exc
