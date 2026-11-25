"""Shared runtime domain types (decision 0020): what the agent, parser, feature extractor
and gates produce and consume.

Neutral by construction. These types carry no evaluator ground truth and import nothing
from the evaluator or harness side (safeops.faults, safeops.experiment, safeops.verify,
safeops.environment, safeops.bootstrap, safeops.analysis). tests/test_information_wall.py
checks that, transitively. The run-record schema (safeops.experiment.schema) reuses these
exact classes for the record's agent-side fields; versioning, strict record validation and
the cross-field rules stay there.

Pydantic, strict: a value of the wrong type is refused, never coerced, and NaN and
infinities are refused in every typed float.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from safeops.enums import ActionType

Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegInt = Annotated[int, Field(ge=0)]

# The one bound on recorded error text: a discard reason's, and the executor's execution
# and step errors (decision 0025 D§16). Neutral, so the executor shares it without reaching
# safeops.experiment.
MAX_DISCARD_ERROR = 2000


class StrictModel(BaseModel):
    """Frozen, extra keys refused, strict types, no NaN or infinity."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class TrajectoryStep(StrictModel):
    """One read-only tool call the agent made (§11.8 trajectory)."""

    step: Annotated[int, Field(ge=1)]
    tool: str
    args: dict[str, Any]
    result: Any
    result_digest: Sha256
    tokens_in: NonNegInt | None
    tokens_out: NonNegInt | None
    latency_ms: Annotated[float, Field(ge=0.0)] | None


class ActionTarget(StrictModel):
    kind: str
    ns: str
    name: str


class RecommendedAction(StrictModel):
    type: ActionType
    target: ActionTarget
    params: dict[str, Any]


class Proposal(StrictModel):
    """The agent's proposal (§11.4). `recommended_action = None` is the no-action proposal."""

    diagnosis: str
    recommended_action: RecommendedAction | None
    confidence: UnitInterval
    evidence: list[NonNegInt]
    prerequisites_claimed: list[str]


class FeatureVector(StrictModel):
    """The frozen risk features (§11.5). The gate takes this and nothing else (Rule 7)."""

    irreversibility: UnitInterval
    blast_radius: UnitInterval
    persistence_risk: UnitInterval
    scope_outside_target: UnitInterval
    confidence_deficit: UnitInterval
    evidence_insufficiency: UnitInterval
    prerequisites_unverified: UnitInterval
    telemetry_conflict: UnitInterval


class _RiskScore(StrictModel):
    risk_score: UnitInterval


class _Confidence(StrictModel):
    confidence: UnitInterval


class _Draw(StrictModel):
    draw: UnitInterval


class ShadowGates(StrictModel):
    """One scalar per shadow strategy (§11.8). Never controls execution."""

    safeops: _RiskScore
    confidence: _Confidence
    random: _Draw
