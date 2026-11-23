"""Milestone 5 acceptance, live (decisions 0017, 0018). Run with `make cluster-test`.

Done when: the validator rejects malformed records (offline, tests/test_m5_records.py),
and every run writes one: shown here against the real cluster, with real provenance. The
agent-side stages don't exist yet (Milestones 7-9), so the one complete run uses
fabricated agent, gate, executor and verifiers. Its executor never touches the cluster.

Records go to a temporary directory, never the repository's data/runs/ (conftest guard).
"""

from __future__ import annotations

import json
import os
import re
import time

import pytest
from typer.testing import CliRunner

from safeops.cluster import CommandError, WorkloadProxy, load_cluster_config
from safeops.environment.manager import EnvironmentManager
from safeops.experiment.records import ProvenanceSource, RunRecordWriter, load_record
from safeops.experiment.runner import build_runner
from safeops.experiment.unit import RunUnit
from safeops.faults.injector import FaultNotPresent
from tests.cluster.test_fault_live import SILENT_SPEC

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFEOPS_CLUSTER_TESTS") != "1",
    reason="needs a live cluster (make cluster-test)",
)

CFG = load_cluster_config()


@pytest.fixture(scope="module")
def env() -> EnvironmentManager:
    e = EnvironmentManager()
    if not e.verify_clean_state().clean:
        e.reset_environment()
    assert e.verify_clean_state().clean, "precondition: start from a clean testbed"
    return e


def _one_record(directory):
    paths = sorted(directory.glob("*.json"))
    assert len(paths) == 1, f"expected exactly one record, found {[p.name for p in paths]}"
    return json.loads(paths[0].read_text()), load_record(paths[0])


def _check_real_provenance(prov: dict) -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", prov["safeops_commit"])
    assert prov["kubernetes_version"] and prov["kubernetes_version"].startswith("v1.")
    assert prov["kind_version"] and prov["kind_version"].startswith("v0.")
    images = json.loads((CFG.state_dir / "images.json").read_text())["built"]
    assert prov["image_digests"] == {k: v["index_digest"] for k, v in images.items()}
    assert prov["is_pilot_run"] is True


# --- 0017 §5: the deployed load generator's run boundary -----------------------------------------


def test_deployed_load_generator_start_is_the_run_boundary() -> None:
    proxy = WorkloadProxy(CFG)
    start = {"action": "start", "rate_rps": 10, "deadline_s": 1.0, "path": "/checkout"}
    proxy.control({"action": "stop"})  # a clean slate
    proxy.control(start)
    try:
        w1 = proxy.control({"action": "measure", "attempts": 20})["window_id"]
        deadline = time.monotonic() + 30
        while proxy.stats(w1).get("state") != "complete":
            assert time.monotonic() < deadline, "first window never completed"
            time.sleep(0.5)
    finally:
        proxy.control({"action": "stop"})
    stopped = proxy.stats()
    assert w1 in stopped["windows"]  # stop does not clear: still inspectable
    assert stopped["dispatched_total"] >= 20

    proxy.control(start)
    try:
        fresh = proxy.stats()
        assert fresh["windows"] == []
        assert fresh["dispatched_total"] < 20  # restarted from zero for the new run
        with pytest.raises(CommandError):
            proxy.stats(w1)  # the previous run's window is gone (404)
        w2 = proxy.control({"action": "measure", "attempts": 20})["window_id"]
        assert int(w2.lstrip("w")) > int(w1.lstrip("w"))  # ids keep increasing
        deadline = time.monotonic() + 30
        while (summary := proxy.stats(w2)).get("state") != "complete":
            assert time.monotonic() < deadline, "second window never completed"
            time.sleep(0.5)
        assert summary["scheduled"] == 20 and summary["succeeded"] == 20
        assert proxy.stats()["windows"] == [w2]
    finally:
        proxy.control({"action": "stop"})


# --- every run writes one --------------------------------------------------------------------


def test_a_real_silent_non_injection_writes_one_valid_discard_record(env, tmp_path) -> None:
    faults = tmp_path / "faults"
    faults.mkdir()
    (faults / "silent-log-level.yaml").write_text(SILENT_SPEC)
    runner = build_runner()
    runner.records = RunRecordWriter(runs_dir=tmp_path / "runs")
    runner.injector._faults_dir = faults
    try:
        with pytest.raises(FaultNotPresent) as exc:
            runner.run(RunUnit("silent-log-level", 0, 0))
        assert any(n.startswith("run record:") for n in exc.value.__notes__)
        assert runner.workload._proxy().stats()["running"] is False  # the run stopped it
    finally:
        env.reset_environment()
        assert env.verify_clean_state().clean
    raw, record = _one_record(tmp_path / "runs")
    assert record.discarded and record.discard_reason.cause == "silent_non_injection"
    assert record.discard_reason.phase == "pre_agent"
    assert raw["steady_state"]["succeeded"] >= 297
    assert raw["fault_verified"] is False and len(raw["fault_verification"]["observations"]) == 3
    assert raw["alert"] is None and raw["trajectory"] is None and raw["outcome"] is None
    _check_real_provenance(raw["provenance"])
    print(f"\n  discard record: {record.discard_reason.cause} at {record.discard_reason.stage}")


def test_a_real_run_with_fabricated_agent_stages_writes_one_complete_record(env, tmp_path) -> None:
    """Every harness stage is real (reset, load, inject, verify, settle, alert, context,
    stabilization, snapshots, cleanup). Only the stages Milestones 7-9 build are fabricated."""
    from tests.test_m5_records import (
        APPLIED,
        EXECUTE,
        FEATURES,
        PROPOSAL,
        SHADOW,
        Agent,
        AgentProvenance,
        Fixed,
        Rec,
        Through,
    )

    real = build_runner()

    class RealPlusAgentProvenance(ProvenanceSource):
        def collect(self):
            p = super().collect()
            fabricated = AgentProvenance().collect()
            for key in (
                "provider",
                "model_id",
                "model_parameters",
                "prompt_hash",
                "tool_schema_hash",
                "action_semantics_hash",
                "gate_feature_version",
                "evidence_requirements_hash",
                "conflict_rules_hash",
            ):
                p[key] = fabricated[key]
            return p

    runner = Through(
        environment=real.environment,
        workload=real.workload,
        injector=real.injector,
        alerts=real.alerts,
        agent=Agent(),
        parser=Fixed(PROPOSAL),
        extractor=Fixed(FEATURES),
        shadow_gates=Fixed(SHADOW),
        gate=Fixed(EXECUTE),
        executor=Fixed(APPLIED),  # fabricated: claims applied, touches nothing
        effectiveness=Fixed(True),
        harm=Fixed(Rec({"harmful": False, "harm_violations": []})),
        records=RunRecordWriter(runs_dir=tmp_path / "runs", provenance=RealPlusAgentProvenance()),
    )
    # Milestone 6 (decision 0023): where the two real clock probes fall in the real run.
    events: list[str] = []
    real_env, real_workload = runner.environment, runner.workload
    traced = {
        (real_env, "read_clock"): lambda: (
            f"read_clock(load running={real_workload._proxy().stats()['running']})"
        ),
        (real_env, "reset_environment"): lambda: "reset_environment",
        (real_workload, "start_workload"): lambda: "start_workload",
    }
    for (owner, name), label in traced.items():
        original = getattr(owner, name)

        def wrapper(*a, _original=original, _label=label, **k):
            events.append(_label())
            return _original(*a, **k)

        setattr(owner, name, wrapper)

    class Harm:
        def check(self, *a, **k):
            events.append("harm")
            return Rec({"harmful": False, "harm_violations": []})

    runner.harm = Harm()
    try:
        runner.run(RunUnit("bad-deployment-config", 0, 0))
    finally:
        for owner, name in traced:
            owner.__dict__.pop(name, None)
    assert events == [
        "reset_environment",  # pre-run reset (then verify)
        "read_clock(load running=False)",  # before the load and the incident begin
        "start_workload",
        "harm",  # the last measurement
        "read_clock(load running=True)",  # measurements complete; nothing destroyed yet
        "reset_environment",  # the post-run cleanup, after the second reading
    ], events
    assert env.verify_clean_state().clean  # the real post-run reset removed the fault
    raw, record = _one_record(tmp_path / "runs")
    assert raw["schema_version"] == 2
    for name in ("clock_before_load", "clock_after_measurements"):
        assert -5 < raw[name]["offset_s"] < 5, raw[name]
    assert raw["clock_change"]["interpretation"] in ("change_detected", "no_change_detected")
    assert not record.discarded and record.in_cohort_g and record.reset_verified is True
    assert raw["fault_verified"] is True and raw["fault"]["variant_index"] == 3
    assert raw["postfault_settledness"]["settled"] is True
    assert raw["alert"]["symptom"] == "http_5xx_rate > 0.2 over 30s"
    assert raw["prefault_snapshot_hash"] != raw["postfault_snapshot_hash"]
    assert raw["stabilization_timed_out"] is False
    _check_real_provenance(raw["provenance"])
    print(
        f"\n  complete record: wall {raw['totals']['wall_seconds']:.0f}s,"
        f" settled in {raw['postfault_settledness']['polls']} polls,"
        f" clock offsets {raw['clock_before_load']['offset_s']}s /"
        f" {raw['clock_after_measurements']['offset_s']}s, change {raw['clock_change']}"
    )


def test_the_acceptance_command_reaching_the_agent_stub_writes_no_record(env, tmp_path) -> None:
    """R1: a stub abort is not an attempted outcome. Records would go to the (patched)
    default directory; the conftest guard also checks the repository's data/runs/."""
    from safeops import cli
    from safeops.experiment import records

    try:
        result = CliRunner().invoke(
            cli.app, ["run", "--fault", "bad-deployment-config", "--seed", "0", "--trial", "0"]
        )
        assert result.exit_code == cli.EXIT_NOT_IMPLEMENTED, result.output
        assert "Agent.investigate" in result.output
        assert not records.RUNS_DIR.exists() or not any(records.RUNS_DIR.glob("*.json"))
        assert WorkloadProxy(CFG).stats()["running"] is False
    finally:
        env.reset_environment()
        assert env.verify_clean_state().clean
