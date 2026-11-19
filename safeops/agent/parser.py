"""Proposal parser (design §11.4). Implemented at Milestone 8."""

from __future__ import annotations

from safeops.stubs import stub


class ProposalParser:
    @stub(milestone=8)
    def parse(self, raw_output):
        """Turn raw model output into a Proposal plus its ProposalStatus (§7.1)."""
