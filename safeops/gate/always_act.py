"""always-act: the Phase A live gate (design §10.2, §10.7; decision 0025 D§6).

The only production live gate in Milestone 8. It executes every valid action and reads no
feature value: Phase A extracts and freezes the vector anyway, for replay (§10.3).
"""

from __future__ import annotations

from safeops.domain import FeatureVector
from safeops.gate.protocol import EXECUTE, GateDecision


class AlwaysActGate:
    name = "always-act"

    def decide(self, features) -> GateDecision:
        """Return GateDecision(execute). Takes a FeatureVector and nothing else (§10.3)."""
        if not isinstance(features, FeatureVector):
            raise TypeError(f"a gate decides from a FeatureVector, not {type(features)}")
        return GateDecision(strategy=self.name, decision=EXECUTE, score=None)
