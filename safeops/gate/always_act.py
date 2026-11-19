"""always-act strategy: the Phase A live gate (design §10.2, §10.7). Implemented at Milestone 8."""

from __future__ import annotations

from safeops.stubs import stub


class AlwaysActGate:
    name = "always-act"

    @stub(milestone=8)
    def decide(self, features):
        """Return GateDecision(execute). Takes a FeatureVector and nothing else (§10.3)."""
