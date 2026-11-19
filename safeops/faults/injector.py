"""Fault injector (design §11.2). Implemented at Milestone 4."""

from __future__ import annotations

from safeops.stubs import stub


class FaultInjector:
    @stub(milestone=4)
    def inject(self, fault_id: str, fault_seed: int):
        """Inject the fault deterministically from (fault_id, fault_seed); return InjectedFault."""

    @stub(milestone=4)
    def verify_fault_present(self, fault) -> bool:
        """Confirm the symptom is observable before the agent starts.

        The timeout_s parameter from §11.2 is added at Milestone 4, along with the decision
        that sets its value.
        """

    @stub(milestone=4)
    def clear(self, fault) -> None:
        """Remove the injected fault."""
