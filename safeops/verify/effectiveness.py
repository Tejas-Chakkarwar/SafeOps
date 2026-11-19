"""Effectiveness verifier (design §11.7). Implemented at Milestone 7."""

from __future__ import annotations

from safeops.stubs import stub


class EffectivenessVerifier:
    @stub(milestone=7)
    def verify(self, steady_state) -> bool:
        """True if the workload success rate returns to within tolerance of the pre-fault
        baseline inside the timeout. The timeout_s parameter is added at Milestone 7, along
        with the decision that sets its value.
        """
