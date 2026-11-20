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
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from safeops.enums import ActionType, ExecutionStatus, ProposalStatus
from safeops.experiment.unit import FAULT_ID_PATTERN, MAX_FAULT_SEED, MAX_TRIAL_INDEX

SCHEMA_VERSION = 1

Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegInt = Annotated[int, Field(ge=0)]

# Where a discarded run stopped, in run order. Everything from "agent" on is post-agent.
PRE_AGENT_STAGES = (
    "pre_run_clean_state",
    "start_workload",
    "steady_state",
    "prefault_snapshot",
    "inject",
    "verify_fault_present",
    "fault_present",
    "postfault_snapshot",
    "alert",
    "runtime_context",
)
POST_AGENT_STAGES = (
    "agent",
    "parse",
    "route_proposal",
    "extract",
    "shadow_gates",
    "gate",
    "route_gate",
    "execute",
    "executed_action",
    "stabilization",
    "postaction_snapshot",
    "effectiveness",
    "harm",
)
DiscardStage = Literal[PRE_AGENT_STAGES + POST_AGENT_STAGES]  # type: ignore[valid-type]
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
]
Category = Literal[
    "correct_autonomy", "harmful_but_effective", "ineffective", "harmful_and_ineffective"
]


class RecordValidationError(ValueError):
    """A run record that doesn't match its declared schema version."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


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
    size: NonNegInt
    scheduled: NonNegInt
    succeeded: NonNegInt | None
    min_successes: NonNegInt
    success_rate: UnitInterval | None
    p95_latency_ms: Annotated[float, Field(ge=0.0)] | None
    failure: str | None


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
    strategy: str
    decision: Literal["execute", "escalate"]
    score: float | None


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
    stage: DiscardStage
    phase: Literal["pre_agent", "post_agent"]
    cause: DiscardCause
    error: str


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
    phase: Literal["A", "do-nothing"]
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
    if (r.trajectory is None) != (r.proposal_status is None):
        p.append("trajectory and proposal_status must be present together")
    if r.alert is not None and (r.fault_verification is None or not r.fault_verification.present):
        p.append("an alert exists although the symptom was not verified to hold")

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
    gate_parts = (r.feature_vector, r.gate, r.shadow_gates)
    if any(x is not None for x in gate_parts) and not all(x is not None for x in gate_parts):
        p.append("feature_vector, shadow_gates and gate must be present together")
    if r.gate is not None and r.proposal_status != ProposalStatus.VALID_ACTION:
        p.append("the gate was consulted for a proposal that was not valid_action")
    if r.execution is not None and (r.gate is None or r.gate.decision != "execute"):
        p.append("an execution exists without a gate decision to execute")

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

    # Discards.
    if r.discarded != (r.discard_reason is not None):
        p.append("discard_reason must be set exactly when discarded is true")
    d = r.discard_reason
    if d is not None:
        post = d.stage in POST_AGENT_STAGES
        if d.phase != ("post_agent" if post else "pre_agent"):
            p.append(f"discard stage {d.stage} is not in phase {d.phase}")
        if d.phase == "pre_agent" and (r.trajectory is not None or r.execution is not None):
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
        if d.cause == "no_steady_state" and r.steady_state is None:
            p.append("no_steady_state needs the steady_state that failed")
    else:
        # A completed run: the whole core chain, measured outcome, cleanup verdict.
        missing = [e for e in _CORE_CHAIN if not present[e]]
        if missing:
            p.append(f"a completed (not discarded) run is missing {missing}")
        if r.outcome is not None and (r.outcome.effective is None or r.outcome.harmful is None):
            p.append("a completed run must have effective and harmful measured")
        if r.reset_verified is None:
            p.append("a completed run must record reset_verified")
        if r.clean_state_verified is False:
            p.append("a completed run cannot have started from a dirty environment")
        if r.fault_verification is not None and not r.fault_verification.present:
            p.append("a completed run cannot follow silent non-injection")

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
