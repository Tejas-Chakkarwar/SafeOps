"""Shared offline fakes. No test here ever touches a cluster."""

from __future__ import annotations

import pytest

from safeops.environment.manager import CleanStateResult
from safeops.environment.snapshot import Snapshot
from safeops.environment.workload import SteadyState


class FakeEnvironment:
    """Stands in for EnvironmentManager: clean, stable, empty snapshots."""

    def __init__(self, clean: bool = True) -> None:
        self.calls: list[str] = []
        self.clean = clean

    def reset_environment(self) -> None:
        self.calls.append("reset_environment")

    def verify_clean_state(self) -> CleanStateResult:
        self.calls.append("verify_clean_state")
        return CleanStateResult(self.clean, () if self.clean else ("Deployment/orders: drift",))

    def capture_snapshot(self) -> Snapshot:
        self.calls.append("capture_snapshot")
        return Snapshot({})

    def wait_stabilization_window(self) -> bool:
        self.calls.append("wait_stabilization_window")
        return True

    def quarantine_and_rebuild(self, reason: str = "") -> None:
        self.calls.append("quarantine_and_rebuild")


class FakeWorkload:
    def start_workload(self) -> None:
        pass

    def verify_steady_state(self) -> SteadyState:
        return SteadyState("w1", "complete", 300, 300, 300, 297, 1.0, 10.0)

    def stop_workload(self) -> None:
        pass


@pytest.fixture
def offline_runner_factory():
    """build_runner() with every implemented, cluster-touching stage replaced by a fake;
    stages that are still stubs stay real, so a run stops at the first real stub."""
    from safeops.experiment import runner as runner_module

    real_build = runner_module.build_runner  # captured before any test monkeypatches it

    def factory():
        r = real_build()
        r.environment = FakeEnvironment()
        r.workload = FakeWorkload()
        return r

    return factory
