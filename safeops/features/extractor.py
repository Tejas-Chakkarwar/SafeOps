"""Risk feature extractor (design §10.3, §11.5). Implemented at Milestone 8."""

from __future__ import annotations

from safeops.stubs import stub


class RiskFeatureExtractor:
    @stub(milestone=8)
    def extract(self, proposal, trajectory, context):
        """Compute and freeze the FeatureVector, even under always-act. Never uses an LLM."""
