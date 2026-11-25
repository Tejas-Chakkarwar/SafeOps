"""The live gate's protocol and its decision (design §10.3; decision 0025 D§4, D§6).

The runner holds a `Gate` (constructor injection) and routes on `GateDecision.decision`
only, so swapping the strategy is passing another object: no other file changes. The gate
receives a FeatureVector and nothing else (Rule 7): no proposal, trajectory, context, run id
or shadow score.

`GateDecision` can say escalate. The version-2 Phase A record deliberately can't (its gate is
always-act / execute / null, §10.2), so an escalating live gate needs its own record form
later (§10.4); production Milestone 8 has only AlwaysActGate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol

from safeops.domain import FeatureVector

EXECUTE = "execute"
ESCALATE = "escalate"


@dataclass(frozen=True)
class GateDecision:
    strategy: str
    decision: Literal["execute", "escalate"]
    score: float | None

    def __post_init__(self) -> None:
        if type(self.strategy) is not str or not self.strategy:
            raise TypeError("GateDecision.strategy must be a non-empty string")
        if self.decision not in (EXECUTE, ESCALATE):
            raise ValueError(
                f"GateDecision.decision must be execute or escalate: {self.decision!r}"
            )
        if self.score is not None and (
            type(self.score) is not float or not math.isfinite(self.score)
        ):
            raise ValueError(f"GateDecision.score must be None or a finite float: {self.score!r}")

    def to_record(self) -> dict:
        return {"strategy": self.strategy, "decision": self.decision, "score": self.score}


class Gate(Protocol):
    name: str

    def decide(self, features: FeatureVector) -> GateDecision:
        """Decide from the FeatureVector alone (Rule 7)."""
        ...
