"""Shadow gate scalars recorded during Phase A (design §10.4, §11.8). Implemented at Milestone 8."""

from __future__ import annotations

from safeops.stubs import stub


class ShadowGateScorer:
    @stub(milestone=8)
    def score(self, features):
        """Return one scalar per strategy: safeops risk_score, confidence, and the random draw.

        The random draw is seeded from run_id (Rule 24). run_id arrives with the run record
        at Milestone 5. No scalar affects execution.
        """
