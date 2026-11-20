"""Shared offline fakes. No test here ever touches a cluster."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safeops.environment.manager import CleanStateResult, SettledSnapshot
from safeops.environment.snapshot import Snapshot
from safeops.environment.workload import SteadyState
from safeops.faults.injector import FaultVerification, SymptomObservation
from safeops.faults.spec import Symptom

FIXED_TIME = datetime(2026, 11, 19, 18, 0, 0, tzinfo=UTC)


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

    def capture_settled_snapshot(self) -> SettledSnapshot:
        self.calls.append("capture_settled_snapshot")
        return SettledSnapshot(Snapshot({}), True, 3, 10.0)

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


class FakeInjector:
    """Injects nothing; reports the declared symptom as measured to hold."""

    def __init__(self, present: bool = True) -> None:
        self.present = present
        self.calls: list[str] = []

    def inject(self, fault_id: str, fault_seed: int):
        self.calls.append("inject")
        return ("fake-injected-fault", fault_id, fault_seed)

    def verify_fault_present(self, fault, timeout_s=None) -> FaultVerification:
        self.calls.append("verify_fault_present")
        symptom = Symptom("HighErrorRate", "orders", "http_5xx_rate > 0.2 over 30s")
        value = 1.0 if self.present else 0.0
        obs = SymptomObservation("w2", 300, int(300 * value), value, self.present, FIXED_TIME)
        return FaultVerification(self.present, symptom, (obs,), None if self.present else "absent")


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
        r.injector = FakeInjector()
        r.alerts._namespace = "testbed"  # no cluster config read in offline tests
        return r

    return factory
