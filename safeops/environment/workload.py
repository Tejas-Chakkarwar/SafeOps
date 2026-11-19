"""Workload generator (design §3, §5). Implemented at Milestone 2.

§13 does not place the workload generator. It lives here, beside the lifecycle code,
because both are harness components that drive the testbed (decision 0004).
"""

from __future__ import annotations

from safeops.stubs import stub


class WorkloadGenerator:
    @stub(milestone=2)
    def start_workload(self) -> None:
        """Start HTTP load against gateway."""

    @stub(milestone=2)
    def verify_steady_state(self):
        """Measure the baseline success rate and latency; return a SteadyState."""

    @stub(milestone=2)
    def stop_workload(self) -> None:
        """Stop the load generator."""
