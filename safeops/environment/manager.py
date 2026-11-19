"""Environment lifecycle and canonical snapshots (design §11.1). Implemented at Milestone 3.

Rule 15: reset, snapshot and quarantine are never faked. Until Milestone 3 they refuse.
"""

from __future__ import annotations

from safeops.stubs import stub


class EnvironmentManager:
    @stub(milestone=3)
    def reset_environment(self) -> None:
        """Restore the testbed namespace to config/desired_state.yaml."""

    @stub(milestone=3)
    def verify_clean_state(self):
        """Check the cluster against desired state and return a CleanStateResult.

        Must also confirm independently that no fault-backend side effect remains,
        such as a netem qdisc or a stress-ng workload. A missing Chaos CR is not
        enough (decision 0003, F3).
        """

    @stub(milestone=3)
    def capture_snapshot(self):
        """Canonical semantic projection of the testbed (§11.1). Never a raw object dump."""

    @stub(milestone=3)
    def wait_stabilization_window(self) -> None:
        """Wait until every testbed deployment has observed generation equal to its
        desired generation with all replicas ready, or until 120 s pass (§5).
        """

    @stub(milestone=3)
    def quarantine_and_rebuild(self) -> None:
        """Stop using this worker, recreate the namespace or cluster, then verify desired state."""
