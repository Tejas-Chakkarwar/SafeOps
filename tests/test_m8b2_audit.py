"""Milestone 8B-2, offline: the executor audit trail and the executor's audited lifecycle
(decision 0025 D§11.8, M8B-2 amendment): the shared, rebind-safe sink; binding before any
filesystem work; directory and file durability; the before_write gate; the after_write and
final evidence-preservation barriers; exactly one final event per returned result.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from safeops.enums import ExecutionStatus
from safeops.executor import audit as audit_module
from safeops.executor.audit import (
    AuditPersistenceError,
    HarnessMisuse,
    JsonlExecutorAudit,
    OsFs,
    canonical_run_id,
)
from safeops.executor.executor import HARNESS_BUGS, ActionExecutor
from safeops.executor.kubernetes import KubernetesReaderTransport, KubernetesWriterTransport
from tests.m8_executor_fakes import (
    NS,
    RUN_A,
    RUN_B,
    SEMANTICS,
    FakeCluster,
    FakeWriter,
    RecordingAudit,
    Rig,
    patch_configmap,
    resources,
    restart,
    rollback,
    scale,
    seed_objects,
)
from tests.m8_fixtures import TOPOLOGY
from tests.m8b2_fakes import ProcessCluster, credentials, scratch_cfg

S = ExecutionStatus
POOL = patch_configmap({"max_pool_size": 20})


class RecordingFs(OsFs):
    """The real filesystem calls, each recorded first (so an order can be proven)."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def _note(self, op: str, path) -> None:
        self.calls.append((op, str(path)))
        if op == self.fail:
            raise OSError(f"injected {op} failure")

    def is_dir(self, path):
        self._note("is_dir", path)
        return super().is_dir(path)

    def lexists(self, path):
        self._note("lexists", path)
        return super().lexists(path)

    def mkdir(self, path):
        self._note("mkdir", path)
        super().mkdir(path)

    def open(self, path, flags, mode):
        self._note("open_new" if flags & os.O_EXCL else "open_append", path)
        return super().open(path, flags, mode)

    def write(self, fd, data):
        self._note("write", fd)
        return super().write(fd, data)

    def fsync(self, fd):
        self._note("fsync", fd)
        super().fsync(fd)

    def close(self, fd):
        self._note("close", fd)
        super().close(fd)

    def fsync_dir(self, path):
        self._note("fsync_dir", path)
        super().fsync_dir(path)


def _lines(sink: JsonlExecutorAudit, run_id: str = RUN_A) -> list[dict]:
    path = sink.path_for(run_id)
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# --- binding: the one HarnessMisuse source, before any filesystem work --------------------------


@pytest.mark.parametrize("event", ["before_write", "after_write", "read", "final"])
def test_an_unbound_append_is_harness_misuse_before_any_filesystem_call(tmp_path, event) -> None:
    fs = RecordingFs()
    sink = JsonlExecutorAudit(tmp_path / "audit", fs=fs)
    rig = Rig()
    rig.apply(scale(4))
    request, result = rig.writer.writes[0], rig.apply(scale(5))
    calls = {
        "before_write": lambda: sink.before_write(request, "x"),
        "after_write": lambda: sink.after_write(request, rig.writer.writes and _reply()),
        "read": lambda: sink.read(purpose="reconciliation", attempt=1),
        "final": lambda: sink.final(result),
    }
    with pytest.raises(HarnessMisuse):
        calls[event]()
    assert fs.calls == [] and not (tmp_path / "audit").exists()


def _reply():
    from safeops.executor.transport import WriteClass, WriteReply

    return WriteReply(WriteClass.SUCCESS)


def test_an_unbound_sink_is_misuse_even_when_persistence_would_fail(tmp_path) -> None:
    """Sink level, the same read append twice over a filesystem whose directory creation
    always fails (injected through the sink's filesystem seam, so it fails under any uid).
    Unbound: the binding guard wins, and persistence is never reached. Bound: persistence is
    reached, and its failure is an AuditPersistenceError."""
    fs = RecordingFs(fail="mkdir")
    root = tmp_path / "audit"
    sink = JsonlExecutorAudit(root, fs=fs)

    def failing_calls() -> int:
        return sum(op == "mkdir" for op, _ in fs.calls)

    with pytest.raises(HarnessMisuse):
        sink.read(purpose="container_check", attempt=1)
    assert fs.calls == [] and failing_calls() == 0  # no filesystem work before the guard
    assert not root.exists()

    sink.bind_run(RUN_A)
    with pytest.raises(AuditPersistenceError, match="injected mkdir failure"):
        sink.read(purpose="container_check", attempt=1)
    assert failing_calls() >= 1  # the persistence path really ran
    assert not root.exists()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        42,
        "",
        "not-a-uuid",
        RUN_A.upper(),
        "{" + RUN_A + "}",
        "urn:uuid:" + RUN_A,
        RUN_A.replace("-", ""),
        "../" + RUN_A,
    ],
)
def test_bind_run_takes_a_canonical_uuid_only_and_touches_nothing(tmp_path, bad) -> None:
    fs = RecordingFs()
    sink = JsonlExecutorAudit(tmp_path / "audit", fs=fs)
    with pytest.raises(ValueError):
        sink.bind_run(bad)
    with pytest.raises(HarnessMisuse):
        sink.require_bound()
    sink.bind_run(RUN_A)
    assert fs.calls == [] and not (tmp_path / "audit").exists()
    assert canonical_run_id(RUN_A) == RUN_A


def test_the_default_root_is_read_at_construction(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_module, "EXECUTOR_AUDIT_DIR", tmp_path / "elsewhere")
    sink = JsonlExecutorAudit()
    assert sink.root == tmp_path / "elsewhere"
    assert audit_module.REPO_ROOT / "data" / "executor_audit" != sink.root


# --- durability --------------------------------------------------------------------------------


def test_new_directories_and_the_new_run_file_are_made_durable_in_order(tmp_path) -> None:
    fs = RecordingFs()
    root = tmp_path / "data" / "executor_audit"  # neither exists; tmp_path does
    sink = JsonlExecutorAudit(root, fs=fs)
    sink.bind_run(RUN_A)
    sink.read(purpose="container_check", attempt=1)
    ops = [(op, p) for op, p in fs.calls if op not in ("is_dir", "lexists")]
    file = str(root / f"{RUN_A}.jsonl")
    assert ops[:4] == [
        ("mkdir", str(tmp_path / "data")),
        ("mkdir", str(root)),
        ("fsync_dir", str(tmp_path / "data")),  # the entry for executor_audit
        ("fsync_dir", str(tmp_path)),  # the entry for data: the first existing ancestor
    ]
    assert [op for op, _ in ops[4:]] == ["open_new", "write", "fsync", "close", "fsync_dir"]
    assert ops[4][1] == file and ops[-1] == ("fsync_dir", str(root))
    assert not any(op == "fsync_dir" and p == str(tmp_path.parent) for op, p in fs.calls)
    fs.calls.clear()
    sink.read(purpose="container_check", attempt=2)  # existing file, existing directory
    ops = [op for op, _ in fs.calls if op not in ("is_dir", "lexists")]
    # the exclusive create finds the file, so it is appended to; no directory fsync again
    assert ops == ["open_new", "open_append", "write", "fsync", "close"]
    assert [x["attempt"] for x in _lines(sink)] == [1, 2]


@pytest.mark.parametrize("op", ["mkdir", "open_new", "write", "fsync", "fsync_dir"])
def test_a_filesystem_failure_on_a_bound_sink_is_a_persistence_error(tmp_path, op) -> None:
    sink = JsonlExecutorAudit(tmp_path / "audit", fs=RecordingFs(fail=op))
    sink.bind_run(RUN_A)
    with pytest.raises(AuditPersistenceError, match="injected"):
        sink.read(purpose="container_check", attempt=1)


def test_an_audit_root_that_is_a_file_is_a_persistence_error(tmp_path) -> None:
    (tmp_path / "audit").write_text("not a directory")
    sink = JsonlExecutorAudit(tmp_path / "audit")
    sink.bind_run(RUN_A)
    with pytest.raises(AuditPersistenceError):
        sink.final(Rig().apply(scale(500)))


# --- one shared sink, rebinding, no cached run id ----------------------------------------------


def _wired(tmp_path, script=(), cluster=None):
    """The production wiring over doubles: one real sink, the real writer and reader."""
    cfg = credentials(scratch_cfg(tmp_path))
    run = ProcessCluster(cluster, script)
    sink = JsonlExecutorAudit(tmp_path / "audit")
    executor = ActionExecutor(
        semantics=SEMANTICS,
        topology=TOPOLOGY,
        namespace=NS,
        reader=KubernetesReaderTransport(cfg, runner=run),
        writer=KubernetesWriterTransport(cfg, sink, runner=run, connect=run.connect),
        audit=sink,
        sleep=lambda s: None,
    )
    return executor, sink, run


def test_the_writer_and_the_executor_must_share_one_sink(tmp_path) -> None:
    cfg = credentials(scratch_cfg(tmp_path))
    with pytest.raises(ValueError, match="share one audit sink"):
        ActionExecutor(
            semantics=SEMANTICS,
            topology=TOPOLOGY,
            namespace=NS,
            reader=KubernetesReaderTransport(cfg),
            writer=KubernetesWriterTransport(cfg, JsonlExecutorAudit(tmp_path / "a")),
            audit=JsonlExecutorAudit(tmp_path / "a"),
        )


def test_rebinding_a_to_b_keeps_each_run_in_its_own_file(tmp_path) -> None:
    executor, sink, run = _wired(tmp_path)
    executor.bind_run(RUN_A)
    assert executor.apply(scale(4)).status is S.APPLIED
    executor.bind_run(RUN_B)
    assert executor.apply(POOL).status is S.APPLIED
    a, b = _lines(sink, RUN_A), _lines(sink, RUN_B)
    assert [x["event"] for x in a] == ["before_write", "after_write", "final"]
    assert [x["event"] for x in b] == ["before_write", "after_write"] * 2 + ["final"]
    assert {x["run_id"] for x in a} == {RUN_A} and {x["run_id"] for x in b} == {RUN_B}
    assert {x["name"] for x in a if "name" in x} == {"orders"}
    assert {x["name"] for x in b if "name" in x} == {"db-pool-config", "orders"}
    assert sorted(p.name for p in sink.root.iterdir()) == sorted(
        [f"{RUN_A}.jsonl", f"{RUN_B}.jsonl"]
    )


def test_the_composite_audit_order_is_before_after_before_after_final(tmp_path) -> None:
    executor, sink, _ = _wired(tmp_path)
    executor.bind_run(RUN_A)
    assert executor.apply(POOL).status is S.APPLIED
    lines = _lines(sink)
    assert [(x["event"], x.get("name")) for x in lines] == [
        ("before_write", "db-pool-config"),
        ("after_write", "db-pool-config"),
        ("before_write", "orders"),
        ("after_write", "orders"),
        ("final", None),
    ]
    assert lines[0]["identity"] == "system:serviceaccount:safeops-system:safeops-executor"
    assert lines[1]["response_class"] == "success"
    assert lines[-1]["status"] == "applied"
    assert [s["status"] for s in lines[-1]["steps"]] == ["applied", "applied"]


# --- the executor's lifecycle: binding at entry -------------------------------------------------


@pytest.mark.parametrize(
    "act", [restart(), scale(4), POOL, rollback(1), resources(limits={"cpu_millis": 500})]
)
def test_an_unbound_executor_raises_before_any_read_preflight_write_or_final(act) -> None:
    rig = Rig(audit=RecordingAudit(run_id=None))
    with pytest.raises(HarnessMisuse):
        rig.apply(act)
    assert rig.untouched and rig.audit.attempts == []


def test_an_unbound_common_envelope_refusal_is_loud_too() -> None:
    rig = Rig(audit=RecordingAudit(run_id=None))
    with pytest.raises(HarnessMisuse):
        rig.apply(scale(500))  # would be refused_by_envelope: never a silent, unaudited result


def test_harness_misuse_is_a_harness_bug() -> None:
    assert HarnessMisuse in HARNESS_BUGS


def test_harness_misuse_at_the_first_write_propagates(tmp_path) -> None:
    class Misused(FakeWriter):
        def write(self, request):
            self.writes.append(request)
            raise HarnessMisuse("the writer's sink is unbound")

    rig = Rig()
    rig.writer = Misused(rig.cluster)
    rig.executor.writer = rig.writer
    with pytest.raises(HarnessMisuse):
        rig.apply(restart())
    assert "final" not in rig.audit.kinds(attempted=True)


def test_harness_misuse_at_step_two_is_skipped_with_its_real_cause() -> None:
    class Misused(FakeWriter):
        def write(self, request):
            if self.writes:
                self.writes.append(request)
                raise HarnessMisuse("the writer's sink is unbound")
            return super().write(request)

    rig = Rig()
    rig.writer = Misused(rig.cluster)
    rig.executor.writer = rig.writer
    result = rig.apply(POOL)
    assert result.status is S.PARTIALLY_APPLIED
    assert [s.status for s in result.steps] == ["applied", "skipped"]
    assert "not attempted before send: HarnessMisuse" in result.error
    assert "adapter contract violation" not in result.error  # not falsely attributed


def test_a_guard_refusal_at_step_two_keeps_its_contract_wording() -> None:
    rig = Rig(["success", "guard"])
    result = rig.apply(POOL)
    assert "not attempted before send" in result.error
    assert "adapter contract violation (preflight owns it): GuardError" in result.error


# --- final: exactly one attempt per returned result; none for a raise ---------------------------


def _finals(audit: RecordingAudit, *, attempted: bool = True) -> int:
    return audit.kinds(attempted=attempted).count("final")


def test_f1_a_common_envelope_refusal_gets_exactly_one_final(tmp_path) -> None:
    rig = Rig()
    result = rig.apply(restart(name="postgres"))  # E3: not a whitelisted Deployment
    assert result.status is S.REFUSED_BY_ENVELOPE
    assert rig.audit.kinds() == ["final"] and _finals(rig.audit) == 1
    assert rig.audit.events[0][2]["result"] is result
    executor, sink, _ = _wired(tmp_path)
    executor.bind_run(RUN_A)
    executor.apply(scale(500))
    assert [x["event"] for x in _lines(sink)] == ["final"]


def test_f2_a_failed_pre_write_read_ends_its_audit_with_one_final(tmp_path) -> None:
    rig = Rig(unavailable=3)
    result = rig.apply(resources(limits={"cpu_millis": 500}))
    assert result.status is S.INFRASTRUCTURE_FAILURE and result.steps == ()
    assert rig.audit.kinds() == ["read", "read", "read", "final"]
    assert [f["purpose"] for e, f in rig.audit.attempts if e == "read"] == ["container_check"] * 3
    refused = Rig()
    refused.apply(rollback(9))  # read, refused: revision 9 is not in the history
    assert refused.audit.kinds() == ["read", "read", "final"]


def test_f2_through_the_real_sink_the_file_ends_in_final(tmp_path) -> None:
    executor, sink, _ = _wired(tmp_path)
    executor.bind_run(RUN_A)
    result = executor.apply(resources(container="nope", limits={"cpu_millis": 500}))
    assert result.status is S.REFUSED_BY_ENVELOPE
    lines = _lines(sink)
    assert [x["event"] for x in lines] == ["read", "final"]
    assert lines[0]["purpose"] == "container_check" and lines[0]["outcome"] == "found"


def test_f3_a_normal_result_gets_exactly_one_final() -> None:
    for act, n_writes in ((scale(4), 1), (POOL, 2)):
        rig = Rig()
        assert rig.apply(act).status is S.APPLIED
        assert _finals(rig.audit) == 1 and rig.audit.kinds()[-1] == "final"
        assert len(rig.writer.writes) == n_writes


@pytest.mark.parametrize(
    "failure",
    [
        HarnessMisuse("late"),
        AuditPersistenceError("disk full"),
        OSError("raw"),
        RuntimeError("bug"),
    ],
    ids=["misuse", "persistence", "oserror", "other"],
)
def test_a_failed_final_never_changes_or_suppresses_the_result(failure) -> None:
    baseline = Rig().apply(POOL)
    rig = Rig(audit=RecordingAudit(fail={"final": failure}))
    result = rig.apply(POOL)
    assert result == baseline  # same status, steps and error
    assert _finals(rig.audit) == 1 and _finals(rig.audit, attempted=False) == 0  # no retry
    assert len(rig.writer.writes) == 2  # no additional write


def test_a_failed_final_on_a_refusal_still_returns_it() -> None:
    rig = Rig(audit=RecordingAudit(fail={"final": AuditPersistenceError("disk full")}))
    result = rig.apply(scale(500))
    assert result.status is S.REFUSED_BY_ENVELOPE and _finals(rig.audit) == 1


def test_a_real_final_persistence_failure_returns_the_result(tmp_path) -> None:
    executor, sink, run = _wired(tmp_path)
    executor.bind_run(RUN_A)
    sink._fs = RecordingFs(fail="open_append")  # the run file exists after the writes
    result = executor.apply(scale(4))
    assert result.status is S.APPLIED and len(run.patches) == 1


def test_f4_a_raising_apply_makes_no_final_attempt() -> None:
    rig = Rig(preflight_raises=RuntimeError("preflight bug"))
    with pytest.raises(RuntimeError):
        rig.apply(scale(4))
    assert _finals(rig.audit) == 0
    rig = Rig(audit=RecordingAudit(run_id=None))
    with pytest.raises(HarnessMisuse):
        rig.apply(scale(4))
    assert rig.audit.attempts == []


def test_an_interrupt_in_final_propagates() -> None:
    rig = Rig(audit=RecordingAudit(fail={"final": KeyboardInterrupt()}))
    with pytest.raises(KeyboardInterrupt):
        rig.apply(scale(4))


# --- read events: never a gate ---------------------------------------------------------------


def test_a_pre_write_read_entry_failure_never_stops_the_remediation() -> None:
    rig = Rig(audit=RecordingAudit(fail={"read": AuditPersistenceError("disk full")}))
    result = rig.apply(resources(limits={"cpu_millis": 500}))
    assert result.status is S.APPLIED and len(rig.writer.writes) == 1
    assert rig.audit.kinds(attempted=True) == ["read", "final"]


def test_a_reconciliation_read_entry_failure_never_changes_reconciliation() -> None:
    for failure in (AuditPersistenceError("disk full"), HarnessMisuse("late"), RuntimeError("x")):
        rig = Rig(["ambiguous"], audit=RecordingAudit(fail={"read": failure}))
        assert rig.apply(scale(4)).status is S.APPLIED  # reconciled confirmed
        assert len(rig.writer.writes) == 1


def test_reconciliation_entries_carry_the_compared_fields_only() -> None:
    rig = Rig(["ambiguous"], unavailable=[True, False])
    assert rig.apply(scale(4)).status is S.APPLIED
    reads = [f for e, f in rig.audit.attempts if e == "read"]
    assert [r["outcome"] for r in reads] == ["unavailable", "found"]
    assert reads[0]["reconciliation"] == {"outcome": "unavailable"}
    view = reads[1]["reconciliation"]
    assert view == {
        "outcome": "confirmed",
        "identity": ["Deployment", "orders", "testbed"],
        "check": "replicas",
        "observed": 4,
    }
    assert "spec" not in json.dumps(view)


def test_a_rollback_reconciliation_records_a_template_digest_not_the_template() -> None:
    rig = Rig(["lost"])
    rig.apply(rollback(1))
    view = [f for e, f in rig.audit.attempts if e == "read"][-1]["reconciliation"]
    assert view["outcome"] == "absent" and view["observed"].startswith("sha256:")


# --- after_write through the real writer: a landed write is represented truthfully -------------


@pytest.mark.parametrize(
    "failure", [HarnessMisuse("late"), AuditPersistenceError("disk full")], ids=["misuse", "fs"]
)
def test_an_after_write_failure_after_a_sent_patch_is_still_applied(tmp_path, failure) -> None:
    cfg = credentials(scratch_cfg(tmp_path))
    cluster = FakeCluster(seed_objects())
    run = ProcessCluster(cluster)
    sink = RecordingAudit(fail={"after_write": failure})
    executor = ActionExecutor(
        semantics=SEMANTICS,
        topology=TOPOLOGY,
        namespace=NS,
        reader=KubernetesReaderTransport(cfg, runner=run),
        writer=KubernetesWriterTransport(cfg, sink, runner=run, connect=run.connect),
        audit=sink,
    )
    result = executor.apply(scale(7))
    assert result.status is S.APPLIED
    assert cluster.get("Deployment", "orders")["spec"]["replicas"] == 7
    assert len(run.patches) == 1  # no second write
    assert sink.kinds(attempted=True) == ["before_write", "after_write", "final"]


def test_a_before_write_failure_through_the_real_writer_is_an_unsent_infra_failure(
    tmp_path,
) -> None:
    executor, sink, run = _wired(tmp_path)
    executor.bind_run(RUN_A)
    sink._fs = RecordingFs(fail="open_new")
    result = executor.apply(scale(7))
    assert result.status is S.INFRASTRUCTURE_FAILURE and result.steps == ()
    assert run.patches == [] and "durable" in result.error


def test_a_missing_executor_credential_through_apply_is_an_unsent_infra_failure(tmp_path):
    cfg = credentials(scratch_cfg(tmp_path), executor="missing")
    run = ProcessCluster()
    sink = RecordingAudit()
    executor = ActionExecutor(
        semantics=SEMANTICS,
        topology=TOPOLOGY,
        namespace=NS,
        reader=KubernetesReaderTransport(cfg, runner=run),
        writer=KubernetesWriterTransport(cfg, sink, runner=run, connect=run.connect),
        audit=sink,
    )
    result = executor.apply(POOL)
    assert result.status is S.INFRASTRUCTURE_FAILURE and result.steps == ()
    assert run.calls == []
    assert sink.kinds() == ["before_write", "after_write", "final"]


def test_a_real_422_through_the_writer_is_rejected_and_unchanged(tmp_path) -> None:
    executor, sink, run = _wired(tmp_path, script=["invalid"])
    executor.bind_run(RUN_A)
    before = run.cluster.get("Deployment", "orders")
    result = executor.apply(resources(requests={"memory_mib": 2048}, limits={"memory_mib": 1024}))
    assert result.status is S.REJECTED_INVALID_ACTION
    assert [s.status for s in result.steps] == ["failed"]
    assert run.cluster.get("Deployment", "orders") == before
    assert [x["event"] for x in _lines(sink)] == ["read", "before_write", "after_write", "final"]


def test_an_ambiguous_write_is_reconciled_through_the_reader_without_a_retry(tmp_path) -> None:
    executor, sink, run = _wired(tmp_path, script=["timeout"])
    executor.bind_run(RUN_A)
    assert executor.apply(scale(6)).status is S.APPLIED
    assert len(run.patches) == 1 and len(run.gets) == 1
    events = [(x["event"], x.get("purpose")) for x in _lines(sink)]
    assert events == [
        ("before_write", None),
        ("after_write", None),
        ("read", "reconciliation"),
        ("final", None),
    ]


# --- the runner binds the shared sink to the run --------------------------------------------------


def test_the_runner_binds_the_executor_s_sink_to_the_record_s_run_id(tmp_path) -> None:
    from safeops.stubs import NotImplementedStage
    from tests.test_m5_records import UNIT
    from tests.test_m8_integration import _runner, unwired_executor

    executor = unwired_executor()  # its unwired preflight is a loud stub after the bind
    runner = _runner(tmp_path, executor=executor)
    with pytest.raises(NotImplementedStage):
        runner.run(UNIT)
    assert executor.audit._run_id == runner.current_run_id
    assert executor.audit.attempts == []  # the stub raised before any audited operation


def test_build_runner_is_free_of_credentials_clients_and_files(tmp_path, monkeypatch) -> None:
    import dataclasses
    import subprocess

    from safeops import cluster
    from safeops.experiment import runner as runner_module

    real = cluster.load_cluster_config()
    nowhere = dataclasses.replace(
        real,
        executor_kubeconfig_path=tmp_path / "absent.kubeconfig",
        executor_token_meta_path=tmp_path / "absent.json",
        reader_kubeconfig_path=tmp_path / "absent-reader.kubeconfig",
        reader_token_meta_path=tmp_path / "absent-reader.json",
    )
    monkeypatch.setattr(cluster, "load_cluster_config", lambda *a, **k: nowhere)

    def no_process(*a, **k):
        raise AssertionError("build_runner ran a process")

    monkeypatch.setattr(subprocess, "run", no_process)
    r = runner_module.build_runner()
    assert r.executor.writer._client is None and r.executor.reader._kubectl is None
    assert not Path(r.executor.audit.root).exists()
    with pytest.raises(HarnessMisuse):
        r.executor.audit.require_bound()
