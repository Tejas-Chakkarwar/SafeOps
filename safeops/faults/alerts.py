"""Alert generator (design §6). Implemented at Milestone 4."""

from __future__ import annotations

from safeops.stubs import stub


class AlertGenerator:
    @stub(milestone=4)
    def build_alert(self, fault):
        """Build an Alert from the fault spec's ``symptoms:`` field and nothing else."""
