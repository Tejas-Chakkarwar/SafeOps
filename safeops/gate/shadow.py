"""Shadow gate scalars recorded during Phase A (design §10.4, §11.8). Implemented at Milestone 8."""

from __future__ import annotations

from safeops.stubs import stub


class ShadowGateScorer:
    @stub(milestone=8)
    def score(self, features, run_id: str):
        """Return one scalar per strategy: safeops risk_score, confidence, and the random draw.

        The random draw is seeded from ``run_id`` (Rule 24), which the runner allocates once
        at the start of the run and passes here (decision 0018, M5 review item 1). No scalar
        affects execution.
        """
