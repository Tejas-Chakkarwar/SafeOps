"""Shared offline fakes. No test here ever touches a cluster."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from safeops.environment.manager import CleanStateResult, SettledSnapshot
from safeops.environment.snapshot import Snapshot
from safeops.environment.workload import SteadyState, WindowMeasurement, load_workload_settings
from safeops.experiment.records import RunRecordWriter
from safeops.faults.injector import (
    FaultBackendRef,
    FaultVerification,
    InjectedFault,
    SymptomObservation,
)
from safeops.faults.spec import Symptom, load_fault_spec
from safeops.verify.effectiveness import EffectivenessVerifier

FIXED_TIME = datetime(2026, 11, 19, 18, 0, 0, tzinfo=UTC)

REPO_RUNS_DIR = Path(__file__).resolve().parent.parent / "data" / "runs"
REPO_STATE_DIR = Path(__file__).resolve().parent.parent / ".safeops"


def scratch_cluster_config(state: Path):
    """The real cluster config with every local path (all of cluster._PATH_KEYS) moved into
    `state`, so code under test that writes or removes local state never touches the real
    .safeops/ (found at Milestone 6: a fixture that missed the new reader paths let offline
    teardown tests delete the real reader credential)."""
    import dataclasses

    from safeops.cluster import _PATH_KEYS, load_cluster_config

    real = load_cluster_config()
    return dataclasses.replace(real, **{k: state / getattr(real, k).name for k in _PATH_KEYS})


def _state_listing() -> dict:
    if not REPO_STATE_DIR.exists():
        return {}
    return {
        str(p.relative_to(REPO_STATE_DIR)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in REPO_STATE_DIR.rglob("*")
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _never_touch_the_real_local_state():
    """No offline test may create, change or remove anything in the real .safeops/ (the
    cluster's credentials and ownership record). Live tests (tests/cluster) are exempt:
    the quarantine test rebuilds the cluster for real."""
    import os

    if os.environ.get("SAFEOPS_CLUSTER_TESTS") == "1":
        yield
        return
    before = _state_listing()
    yield
    assert _state_listing() == before, "an offline test changed the real .safeops/"


def _repo_records() -> set[str]:
    return {p.name for p in REPO_RUNS_DIR.rglob("*")} if REPO_RUNS_DIR.exists() else set()


@pytest.fixture(autouse=True)
def _never_write_the_real_dataset(tmp_path, monkeypatch):
    """No test may ever write a record into the repository's data/runs/ (the dataset).
    Default writers point at a temporary directory, and the dataset is checked untouched."""
    from safeops.experiment import records

    before = _repo_records()
    monkeypatch.setattr(records, "RUNS_DIR", tmp_path / "default-runs")
    yield
    assert _repo_records() == before, "a test wrote into the repository's data/runs/"


def clock_reading(offset_s: int = 0, at: datetime = FIXED_TIME):
    """A fabricated host/cluster clock reading (decision 0023): a 40 ms create round trip
    starting at `at`, with the server clock `offset_s` whole seconds ahead."""
    from datetime import timedelta

    from safeops.environment.clock import ClockReading

    return ClockReading(
        at + timedelta(milliseconds=500),
        at + timedelta(milliseconds=540),
        at + timedelta(seconds=offset_s),
    )


class FakeEnvironment:
    """Stands in for EnvironmentManager: clean, stable, empty snapshots, steady clocks."""

    def __init__(self, clean: bool = True) -> None:
        self.calls: list[str] = []
        self.clean = clean

    def read_clock(self):
        self.calls.append("read_clock")
        return clock_reading()

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
    settings = load_workload_settings()  # the real config: the runner reads its recovery budget

    def start_workload(self) -> None:
        pass

    def verify_steady_state(self) -> SteadyState:
        return SteadyState("w1", "complete", 300, 300, 300, 297, 1.0, 10.0)

    def stop_workload(self) -> None:
        pass

    def measure_recovery_window(self) -> WindowMeasurement:
        """A complete, full-size window in which every attempt succeeded."""
        n = self.settings.window_attempts
        summary = {
            "state": "complete",
            "size": n,
            "scheduled": n,
            "succeeded": n,
            "failed": 0,
            "deadline_s": self.settings.attempt_deadline_s,
        }
        return WindowMeasurement("wr", "complete", n, n, summary)


class FakeInjector:
    """Injects nothing; reports the declared symptom as measured to hold."""

    def __init__(self, present: bool = True) -> None:
        self.present = present
        self.calls: list[str] = []

    def inject(self, fault_id: str, fault_seed: int):
        self.calls.append("inject")
        spec = load_fault_spec(fault_id)
        index, variant = spec.variant_for(fault_seed)
        return InjectedFault(
            spec,
            fault_seed,
            index,
            variant,
            "http://inventory-svc:8080",
            FIXED_TIME,
            FaultBackendRef(spec.injection.backend, None),
        )

    def verify_fault_present(self, fault, timeout_s=None) -> FaultVerification:
        self.calls.append("verify_fault_present")
        symptom = Symptom("HighErrorRate", "orders", "http_5xx_rate > 0.2 over 30s")
        value = 1.0 if self.present else 0.0
        obs = SymptomObservation("w2", 300, int(300 * value), value, self.present, FIXED_TIME)
        return FaultVerification(self.present, symptom, (obs,), None if self.present else "absent")


class FixedProvenance:
    """Provenance with no git, kubectl or kind calls: offline tests never touch a cluster."""

    def collect(self) -> dict:
        sha = "sha256:" + "0" * 64
        return {
            "provider": None,
            "model_id": None,
            "model_parameters": None,
            "prompt_hash": None,
            "tool_schema_hash": None,
            "action_semantics_hash": None,
            "gate_feature_version": None,
            "evidence_requirements_hash": None,
            "conflict_rules_hash": None,
            "fault_definition_hash": None,
            "fault_backend": None,
            "desired_state_hash": sha,
            "topology_hash": sha,
            "snapshot_projection_version": 2,
            "safeops_commit": "0" * 40,
            "safeops_tree_dirty": False,
            "kubernetes_version": "v1.37.0",
            "kind_version": "v0.33.0",
            "testbed_version": "safeops-testbed:000000000000",
            "image_digests": {"testbed": sha},
            "running_images": {},
            "is_pilot_run": True,
        }


def offline_writer(runs_dir) -> RunRecordWriter:
    """The real writer and validator, into a temporary directory, with fixed provenance."""
    return RunRecordWriter(runs_dir=runs_dir, provenance=FixedProvenance())


@pytest.fixture
def offline_runner_factory(tmp_path):
    """build_runner() with every implemented, cluster-touching stage replaced by a fake;
    stages that are still stubs stay real, so a run stops at the first real stub."""
    from safeops.experiment import runner as runner_module

    real_build = runner_module.build_runner  # captured before any test monkeypatches it

    def factory():
        r = real_build()
        r.environment = FakeEnvironment()
        r.workload = FakeWorkload()
        # The verifier must measure the runner's workload, never build_runner's real one.
        r.effectiveness = EffectivenessVerifier(workload=r.workload)
        r.injector = FakeInjector()
        r.alerts._namespace = "testbed"  # no cluster config read in offline tests
        r.records = offline_writer(tmp_path / "runs")  # never the repo's data/runs/
        return r

    return factory
