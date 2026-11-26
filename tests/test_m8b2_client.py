"""Milestone 8B-2, offline: the safeops-executor client phase, its credential, the patch-only
guard, and the concrete writer and reader transports (decision 0025 D§11.3, D§11.7, D§14,
M8B-2 amendment). Nothing here touches a cluster: kubectl and docker are ProcessCluster.
"""

from __future__ import annotations

import ast
import json
import subprocess

import pytest

from safeops.cluster import (
    EXECUTOR_RESOURCES,
    REPO_ROOT,
    CredentialError,
    CredentialUnavailable,
    GuardError,
    Kubectl,
    Phase,
    ProcessResult,
    check_object,
    executor_credential,
    kubeconfig_for,
    load_cluster_config,
)
from safeops.enums import ActionType
from safeops.executor.audit import AuditPersistenceError, HarnessMisuse, JsonlExecutorAudit
from safeops.executor.kubernetes import (
    KubernetesReaderTransport,
    KubernetesWriterTransport,
    classify_patch,
    patch_status,
)
from safeops.executor.transport import (
    Check,
    PatchType,
    ReadOutcome,
    WriteClass,
    WriteReply,
    prepared,
)
from tests.m8_executor_fakes import RUN_A, RecordingAudit
from tests.m8b2_fakes import SERVER, ProcessCluster, credentials, response_line, scratch_cfg

CFG = load_cluster_config()


def _scale(name="orders", replicas=4, ns="testbed", kind="Deployment"):
    return prepared(
        ActionType.SCALE_DEPLOYMENT,
        kind,
        ns,
        name,
        PatchType.MERGE,
        {"spec": {"replicas": replicas}},
        Check.REPLICAS,
        replicas,
    )


def _never(*a, **k):
    raise AssertionError("a refused call must never run a process")


def _executor(cfg, runner=_never) -> Kubectl:
    return Kubectl(
        cfg,
        Phase.EXECUTOR,
        runner=runner,
        kubeconfig=cfg.executor_kubeconfig_path,
        verify_identity=False,
    )


# --- Phase.EXECUTOR: scope, credential, no fallthrough ------------------------------------------


@pytest.mark.parametrize("kind", sorted(EXECUTOR_RESOURCES))
def test_the_executor_phase_may_mutate_its_three_kinds_in_testbed(kind) -> None:
    check_object(CFG, Phase.EXECUTOR, kind=kind, name="orders", namespace=CFG.namespace)


@pytest.mark.parametrize(
    "kind", ["StatefulSet", "Secret", "PersistentVolumeClaim", "Pod", "Role", "ReplicaSet"]
)
def test_the_executor_phase_refuses_every_other_kind(kind) -> None:
    with pytest.raises(GuardError, match="may patch only"):
        check_object(CFG, Phase.EXECUTOR, kind=kind, name="x", namespace=CFG.namespace)


def test_the_executor_phase_never_falls_through_to_the_bootstrap_scope() -> None:
    """Without its own branch, check_object's `else` gave a non-runtime phase every bootstrap
    namespace and the recorded cluster scope."""
    for ns in CFG.bootstrap_namespaces[1:] + ("default", "kube-system"):
        with pytest.raises(GuardError, match="executor may not mutate"):
            check_object(CFG, Phase.EXECUTOR, kind="ConfigMap", name="x", namespace=ns)
    with pytest.raises(GuardError, match="cluster-scoped"):
        check_object(
            CFG,
            Phase.EXECUTOR,
            kind="Namespace",
            name="testbed",
            namespace=None,
            cluster_scope=frozenset({("Namespace", "testbed")}),
        )


def test_the_executor_credential_is_its_own_and_nothing_falls_back(tmp_path) -> None:
    cfg = scratch_cfg(tmp_path)
    for path in (cfg.kubeconfig_path, cfg.harness_kubeconfig_path, cfg.reader_kubeconfig_path):
        path.write_text("x")
    for meta in (cfg.harness_token_meta_path, cfg.reader_token_meta_path):
        meta.write_text(json.dumps({"expires_at": 2**40}))
    with pytest.raises(CredentialUnavailable, match="executor credential missing"):
        kubeconfig_for(cfg, Phase.EXECUTOR)  # admin, harness and reader exist: no fallback
    credentials(cfg)
    assert kubeconfig_for(cfg, Phase.EXECUTOR) == cfg.executor_kubeconfig_path
    assert kubeconfig_for(cfg, Phase.BOOTSTRAP) == cfg.kubeconfig_path


def test_the_three_credential_failures_are_distinct(tmp_path) -> None:
    cfg = scratch_cfg(tmp_path)
    with pytest.raises(CredentialUnavailable):  # missing
        executor_credential(cfg)
    credentials(cfg, executor="expired")
    with pytest.raises(CredentialUnavailable, match="expired"):
        executor_credential(cfg)
    cfg.executor_token_meta_path.write_text("{not json")
    with pytest.raises(CredentialError) as unreadable:
        executor_credential(cfg)
    assert not isinstance(unreadable.value, CredentialUnavailable)  # corrupt, not missing
    credentials(cfg)
    for other in (cfg.kubeconfig_path, cfg.harness_kubeconfig_path, cfg.reader_kubeconfig_path):
        other.write_text("x")
        with pytest.raises(CredentialError, match="never falls") as wrong:
            Kubectl(cfg, Phase.EXECUTOR, kubeconfig=other, verify_identity=False)
        assert not isinstance(wrong.value, CredentialUnavailable)
    with pytest.raises(CredentialError, match="executor kubeconfig"):
        Kubectl(cfg, Phase.READER, kubeconfig=cfg.executor_kubeconfig_path, verify_identity=False)


def test_the_scratch_config_moves_the_executor_paths(tmp_path) -> None:
    from safeops.cluster import _PATH_KEYS
    from tests.conftest import REPO_STATE_DIR

    assert {"executor_kubeconfig_path", "executor_token_meta_path"} <= set(_PATH_KEYS)
    cfg = scratch_cfg(tmp_path)
    for key in ("executor_kubeconfig_path", "executor_token_meta_path"):
        assert REPO_STATE_DIR not in getattr(cfg, key).parents


def test_cluster_yaml_names_the_executor_credential_under_the_state_dir() -> None:
    assert CFG.executor_kubeconfig_path.parent == CFG.state_dir
    assert CFG.executor_kubeconfig_path.name == "safeops-executor.kubeconfig"
    assert CFG.executor_token_meta_path.name == "safeops-executor.token.json"


# --- the patch-only client guard ------------------------------------------------------------


def _obj(kind="ConfigMap"):
    return {"apiVersion": "v1", "kind": kind, "metadata": {"name": "x", "namespace": "testbed"}}


@pytest.mark.parametrize(
    "call",
    [
        lambda k: k.read("get", "deployments", "-n", "testbed"),
        lambda k: k.read("auth", "can-i", "get", "pods"),
        lambda k: k.read("api-resources"),
        lambda k: k.read("version"),
        lambda k: k.read_raw("logs", "x"),
        lambda k: k.get_json("deployment", "orders", "-n", "testbed"),
        lambda k: k.raw("GET", "/api/v1/namespaces/testbed/configmaps"),
        lambda k: k.raw("PUT", "/api/v1/namespaces/testbed/configmaps/x", {}),
        lambda k: k.cluster_scoped_kinds(),
        lambda k: k.apply([_obj()]),
        lambda k: k.create([_obj()]),
        lambda k: k.create_returning(_obj()),
        lambda k: k.replace([_obj("Deployment")]),
        lambda k: k.delete("configmap", "ConfigMap", "x", "testbed"),
        lambda k: k.create_token("safeops-executor", "safeops-system", 60),
    ],
    ids=[
        "get",
        "can-i",
        "api-resources",
        "version",
        "logs",
        "get_json",
        "raw-get",
        "raw-put",
        "discovery",
        "apply",
        "create",
        "create_returning",
        "replace",
        "delete",
        "token",
    ],
)
def test_the_executor_client_refuses_everything_but_patch(tmp_path, call) -> None:
    k = _executor(credentials(scratch_cfg(tmp_path)))
    with pytest.raises(GuardError):
        call(k)


@pytest.mark.parametrize("phase", [Phase.RUNTIME, Phase.READER, Phase.BOOTSTRAP])
def test_only_the_executor_phase_patches(tmp_path, phase) -> None:
    k = Kubectl(
        scratch_cfg(tmp_path),
        phase,
        runner=_never,
        kubeconfig=tmp_path / "kc",
        verify_identity=False,
    )
    with pytest.raises(GuardError, match="only the executor"):
        k.patch(
            kind="Deployment",
            name="orders",
            namespace="testbed",
            patch_type="merge",
            body_json="{}",
            timeout_s=5,
        )


@pytest.mark.parametrize(
    "kind, name, ns, patch_type, body",
    [
        ("StatefulSet", "postgres", "testbed", "merge", "{}"),
        ("Secret", "postgres-credentials", "testbed", "merge", "{}"),
        ("Deployment", "orders", "safeops-system", "merge", "{}"),
        ("Deployment", "--all", "testbed", "merge", "{}"),
        ("Deployment", "orders", "testbed", "apply", "{}"),
        ("Deployment", "orders", "testbed", "merge", "not json"),
        ("Deployment", "orders", "testbed", "json", "{}"),
        ("Deployment", "orders", "testbed", "merge", "[]"),
    ],
)
def test_the_patch_guard_refuses_before_any_process(tmp_path, kind, name, ns, patch_type, body):
    k = _executor(credentials(scratch_cfg(tmp_path)))
    with pytest.raises(GuardError):
        k.patch(
            kind=kind, name=name, namespace=ns, patch_type=patch_type, body_json=body, timeout_s=5
        )


def test_the_patch_is_one_structured_command_with_no_discovery_helper(tmp_path, monkeypatch):
    """check_object directly: never _check_objects, cluster_scoped_kinds or api-resources."""
    cfg = credentials(scratch_cfg(tmp_path))
    run = ProcessCluster()
    k = _executor(cfg, run)
    for helper in ("_check_objects", "cluster_scoped_kinds"):
        monkeypatch.setattr(Kubectl, helper, _never)
    out = k.patch(
        kind="Deployment",
        name="orders",
        namespace="testbed",
        patch_type="merge",
        body_json='{"spec":{"replicas":4}}',
        timeout_s=20,
    )
    assert isinstance(out, ProcessResult) and out.returncode == 0
    assert run.calls == [
        [
            "kubectl",
            "--kubeconfig",
            str(cfg.executor_kubeconfig_path),
            "--context",
            "kind-safeops",
            "patch",
            "deployments.apps",
            "orders",
            "-n",
            "testbed",
            "--type",
            "merge",
            "-p",
            '{"spec":{"replicas":4}}',
            "-o",
            "json",
            "-v=6",
            "--request-timeout=20s",
        ]
    ]
    assert not any("api-resources" in a for c in run.calls for a in c)


def test_a_patch_process_outcome_is_returned_never_raised(tmp_path) -> None:
    cfg = credentials(scratch_cfg(tmp_path))
    args = dict(
        kind="Deployment",
        name="orders",
        namespace="testbed",
        patch_type="merge",
        body_json="{}",
        timeout_s=5,
    )
    timed = _executor(cfg, ProcessCluster(script=["timeout"])).patch(**args)
    assert timed.timed_out and timed.returncode is None
    spawned = _executor(cfg, ProcessCluster(script=["spawn"])).patch(**args)
    assert spawned.spawn_error and spawned.returncode is None
    refused = _executor(cfg, ProcessCluster(script=["forbidden"])).patch(**args)
    assert refused.returncode == 1 and "403" in refused.stderr


def test_the_executor_phase_is_built_only_by_its_transport() -> None:
    """Static: Phase.EXECUTOR appears only in the client (cluster.py) and the executor's
    transport (executor/kubernetes.py), never in tools, the agent, the runner or bootstrap."""
    allowed = {"safeops/cluster.py", "safeops/executor/kubernetes.py"}
    users = set()
    for path in (REPO_ROOT / "safeops").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "EXECUTOR":
                if isinstance(node.value, ast.Name) and node.value.id == "Phase":
                    users.add(str(path.relative_to(REPO_ROOT)))
    assert users == allowed


# --- the semantic classification of one patch ---------------------------------------------------

URL = f"{SERVER}/apis/apps/v1/namespaces/testbed/deployments/orders?fieldManager=kubectl-patch"
OBJ = {"kind": "Deployment", "metadata": {"name": "orders", "namespace": "testbed"}}


def _proc(status=None, rc=0, stdout="", extra="", structured=True, url=URL):
    lines = [response_line("GET", f"{SERVER}/api?timeout=32s", 200, structured=structured)]
    if status is not None:
        lines.append(response_line("PATCH", url, status, structured=structured))
    return ProcessResult(rc, stdout, "\n".join(lines) + "\n" + extra)


@pytest.mark.parametrize("structured", [True, False], ids=["structured", "legacy"])
@pytest.mark.parametrize(
    "status, rc, stdout, cls",
    [
        (200, 0, json.dumps(OBJ), WriteClass.SUCCESS),
        (200, 0, "not json", WriteClass.AMBIGUOUS),
        (200, 0, "[]", WriteClass.AMBIGUOUS),
        (200, 1, json.dumps(OBJ), WriteClass.AMBIGUOUS),
        (422, 1, "", WriteClass.INVALID_ACTION),
        (400, 1, "", WriteClass.DEFINITE_FAILURE),
        (401, 1, "", WriteClass.DEFINITE_FAILURE),
        (403, 1, "", WriteClass.DEFINITE_FAILURE),
        (404, 1, "", WriteClass.DEFINITE_FAILURE),
        (409, 1, "", WriteClass.DEFINITE_FAILURE),
        (429, 1, "", WriteClass.DEFINITE_FAILURE),
        (500, 1, "", WriteClass.AMBIGUOUS),
        (503, 1, "", WriteClass.AMBIGUOUS),
        (504, 1, "", WriteClass.AMBIGUOUS),
    ],
)
def test_the_patch_status_decides_the_class(structured, status, rc, stdout, cls) -> None:
    reply = classify_patch(_proc(status, rc, stdout, structured=structured), _scale())
    assert reply.semantic_class is cls
    if cls is WriteClass.SUCCESS:
        assert reply.returned_object == OBJ


def test_a_422_is_invalid_action_with_kubectl_s_own_text() -> None:
    proc = _proc(422, 1, extra='The Deployment "orders" is invalid: spec.template: Invalid value')
    reply = classify_patch(proc, _scale())
    assert reply.semantic_class is WriteClass.INVALID_ACTION
    assert "HTTP 422" in reply.diagnostic and "is invalid" in reply.diagnostic


def test_outcomes_without_a_patch_response() -> None:
    assert (
        classify_patch(ProcessResult(None, timed_out=True), _scale()).semantic_class
        is WriteClass.AMBIGUOUS
    )
    assert (
        classify_patch(
            ProcessResult(None, spawn_error="FileNotFoundError"), _scale()
        ).semantic_class
        is WriteClass.NOT_ATTEMPTED
    )
    refused = ProcessResult(1, "", response_line("GET", f"{SERVER}/api", 401) + "\nUnauthorized")
    assert classify_patch(refused, _scale()).semantic_class is WriteClass.DEFINITE_FAILURE
    lost = ProcessResult(1, "", "The connection to the server was refused")
    assert classify_patch(lost, _scale()).semantic_class is WriteClass.AMBIGUOUS
    quiet = ProcessResult(0, json.dumps(OBJ), "")
    assert classify_patch(quiet, _scale()).semantic_class is WriteClass.SUCCESS


def test_only_this_object_s_patch_response_counts() -> None:
    other = URL.replace("/orders?", "/gateway?")
    assert patch_status(_proc(422, url=other).stderr, _scale()) is None
    assert patch_status(_proc(422).stderr, _scale()) == 422
    assert patch_status(_proc(422).stderr, _scale(name="gateway")) is None


def test_classification_is_total() -> None:
    weird = ProcessResult(0, json.dumps(OBJ), f'verb="PATCH" url="{URL}" status="999 Odd"')
    assert classify_patch(weird, _scale()).semantic_class is WriteClass.AMBIGUOUS
    elsewhere = ProcessResult(0, json.dumps(OBJ), 'verb="PATCH" url="x" status="422 No"')
    assert classify_patch(elsewhere, _scale()).semantic_class is WriteClass.SUCCESS  # not ours


# --- the concrete writer ---------------------------------------------------------------------


def _writer(tmp_path, *, executor="valid", script=(), audit=None, docker_port=None):
    cfg = credentials(scratch_cfg(tmp_path), executor=executor)
    run = ProcessCluster(script=script, **({"docker_port": docker_port} if docker_port else {}))
    audit = audit if audit is not None else RecordingAudit()
    return KubernetesWriterTransport(cfg, audit, runner=run), run, audit


def test_constructing_the_transports_resolves_no_credential_and_runs_nothing(tmp_path) -> None:
    cfg = scratch_cfg(tmp_path)  # no credential file exists at all
    writer = KubernetesWriterTransport(cfg, RecordingAudit(run_id=None), runner=_never)
    reader = KubernetesReaderTransport(cfg, runner=_never)
    assert writer._kubectl is None and reader._kubectl is None


def test_a_preflighted_write_sends_one_patch_and_returns_the_object(tmp_path) -> None:
    writer, run, audit = _writer(tmp_path)
    request = _scale()
    writer.preflight((request,))
    reply = writer.write(request)
    assert reply.semantic_class is WriteClass.SUCCESS
    assert reply.returned_object["spec"]["replicas"] == 4
    assert len(run.patches) == 1
    assert audit.kinds() == ["before_write", "after_write"]
    assert audit.events[0][2]["identity"] == "system:serviceaccount:safeops-system:safeops-executor"


@pytest.mark.parametrize("executor", ["missing", "expired"])
def test_a_missing_or_expired_credential_is_not_attempted(tmp_path, executor) -> None:
    writer, run, audit = _writer(tmp_path, executor=executor)
    request = _scale()
    writer.preflight((request,))  # not raised: a classified cause (D§11.3)
    reply = writer.write(request)
    assert reply.semantic_class is WriteClass.NOT_ATTEMPTED
    assert run.calls == [] and writer._kubectl is None
    assert audit.kinds() == ["before_write", "after_write"]


def test_a_credential_that_expires_after_preflight_is_not_attempted(tmp_path) -> None:
    writer, run, _ = _writer(tmp_path)
    request = _scale()
    writer.preflight((request,))
    writer.cfg.executor_token_meta_path.write_text(json.dumps({"expires_at": 1}))
    assert writer.write(request).semantic_class is WriteClass.NOT_ATTEMPTED
    assert run.patches == []


def test_a_corrupt_kubeconfig_is_loud_in_preflight(tmp_path) -> None:
    writer, run, audit = _writer(tmp_path, executor="corrupt")
    with pytest.raises(CredentialError, match="no usable") as exc:
        writer.preflight((_scale(),))
    assert not isinstance(exc.value, CredentialUnavailable)
    assert run.patches == [] and audit.attempts == []


def test_unreadable_token_metadata_is_loud_in_preflight_not_unavailable(tmp_path) -> None:
    writer, run, audit = _writer(tmp_path)
    writer.cfg.executor_token_meta_path.write_text("{not json")
    with pytest.raises(CredentialError, match="unreadable") as exc:
        writer.preflight((_scale(),))
    assert not isinstance(exc.value, CredentialUnavailable)
    assert run.calls == [] and audit.attempts == []


def test_a_context_pointing_at_another_cluster_is_a_guard_error_in_preflight(tmp_path) -> None:
    writer, run, _ = _writer(tmp_path, docker_port=55555)
    with pytest.raises(GuardError, match="not the API server"):
        writer.preflight((_scale(),))
    assert run.patches == [] and writer._kubectl is None


@pytest.mark.parametrize(
    "requests",
    [(), [_scale()], ("not a request",), (_scale(ns="default"),), (_scale(kind="StatefulSet"),)],
)
def test_preflight_refuses_what_the_executor_must_never_send(tmp_path, requests) -> None:
    writer, run, _ = _writer(tmp_path)
    with pytest.raises(GuardError):
        writer.preflight(requests)
    assert run.calls == []


def test_a_request_that_was_not_preflighted_is_never_sent(tmp_path) -> None:
    writer, run, audit = _writer(tmp_path)
    writer.preflight((_scale(),))
    with pytest.raises(GuardError, match="preflighted"):
        writer.write(_scale(replicas=5))
    assert run.patches == [] and audit.attempts == []


def test_the_before_write_entry_is_durable_before_the_patch_runs(tmp_path) -> None:
    audit = JsonlExecutorAudit(tmp_path / "audit")
    audit.bind_run(RUN_A)
    writer, run, _ = _writer(tmp_path, audit=audit)
    seen = []
    run.on_patch = lambda argv: seen.append(audit.path_for(RUN_A).read_text().splitlines())
    request = _scale()
    writer.preflight((request,))
    writer.write(request)
    assert [json.loads(line)["event"] for line in seen[0]] == ["before_write"]
    lines = [json.loads(x) for x in audit.path_for(RUN_A).read_text().splitlines()]
    assert [x["event"] for x in lines] == ["before_write", "after_write"]
    assert lines[0]["patch"] == {"spec": {"replicas": 4}}
    assert "token" not in audit.path_for(RUN_A).read_text()


def test_a_before_write_that_cannot_persist_is_not_attempted_and_nothing_is_sent(tmp_path):
    audit = RecordingAudit(fail={"before_write": AuditPersistenceError("disk full")})
    writer, run, _ = _writer(tmp_path, audit=audit)
    request = _scale()
    writer.preflight((request,))
    reply = writer.write(request)
    assert reply.semantic_class is WriteClass.NOT_ATTEMPTED and "durable" in reply.diagnostic
    assert run.patches == []


def test_an_unbound_sink_is_harness_misuse_and_nothing_is_sent(tmp_path) -> None:
    root = tmp_path / "unwritable"
    root.mkdir(mode=0o500)
    writer, run, _ = _writer(tmp_path, audit=JsonlExecutorAudit(root))
    request = _scale()
    writer.preflight((request,))
    with pytest.raises(HarnessMisuse):
        writer.write(request)
    assert run.patches == [] and list(root.iterdir()) == []


@pytest.mark.parametrize(
    "failure",
    [HarnessMisuse("late"), AuditPersistenceError("disk full"), RuntimeError("audit bug")],
    ids=["misuse", "persistence", "other"],
)
def test_after_a_sent_patch_the_reply_survives_any_after_write_failure(tmp_path, failure) -> None:
    audit = RecordingAudit(fail={"after_write": failure})
    writer, run, _ = _writer(tmp_path, audit=audit)
    request = _scale()
    writer.preflight((request,))
    reply = writer.write(request)
    assert reply.semantic_class is WriteClass.SUCCESS
    assert reply.returned_object["spec"]["replicas"] == 4
    assert len(run.patches) == 1 and audit.kinds(attempted=True) == ["before_write", "after_write"]


def test_an_interrupt_in_after_write_still_propagates(tmp_path) -> None:
    audit = RecordingAudit(fail={"after_write": KeyboardInterrupt()})
    writer, _, _ = _writer(tmp_path, audit=audit)
    request = _scale()
    writer.preflight((request,))
    with pytest.raises(KeyboardInterrupt):
        writer.write(request)


@pytest.mark.parametrize(
    "script, cls",
    [
        ("invalid", WriteClass.INVALID_ACTION),
        ("forbidden", WriteClass.DEFINITE_FAILURE),
        ("server", WriteClass.AMBIGUOUS),
        ("timeout", WriteClass.AMBIGUOUS),
        ("spawn", WriteClass.NOT_ATTEMPTED),
        ("raise", WriteClass.AMBIGUOUS),
    ],
)
def test_every_process_outcome_is_a_reply_with_an_after_entry(tmp_path, script, cls) -> None:
    writer, run, audit = _writer(tmp_path, script=[script])
    request = _scale()
    writer.preflight((request,))
    reply = writer.write(request)
    assert isinstance(reply, WriteReply) and reply.semantic_class is cls
    assert audit.kinds() == ["before_write", "after_write"]


# --- the concrete reader ---------------------------------------------------------------------


def _reader(tmp_path, cluster_runner=None):
    cfg = credentials(scratch_cfg(tmp_path))
    run = cluster_runner or ProcessCluster()
    return KubernetesReaderTransport(cfg, runner=run), run


def test_a_get_is_one_reader_process_found_or_absent(tmp_path) -> None:
    reader, run = _reader(tmp_path)
    found = reader.get("Deployment", "orders", "testbed", 10)
    assert found.outcome is ReadOutcome.FOUND and found.obj["metadata"]["name"] == "orders"
    absent = reader.get("Deployment", "nope", "testbed", 10)
    assert absent.outcome is ReadOutcome.ABSENT
    assert run.gets[0][5:] == [
        "get",
        "deployments.apps",
        "orders",
        "-n",
        "testbed",
        "-o",
        "json",
        "--ignore-not-found",
    ]
    assert str(reader.cfg.reader_kubeconfig_path) == run.gets[0][2]
    assert len(run.gets) == 2  # one process per call


def test_a_list_is_one_unpaginated_reader_process(tmp_path) -> None:
    reader, run = _reader(tmp_path)
    listed = reader.list("ReplicaSet", "testbed", 10)
    assert listed.outcome is ReadOutcome.FOUND and listed.obj["items"]
    assert run.gets[0][5:] == [
        "get",
        "replicasets.apps",
        "-n",
        "testbed",
        "-o",
        "json",
        "--chunk-size=0",
    ]


@pytest.mark.parametrize(
    "metadata",
    [{"continue": "token"}, {"remainingItemCount": 3}],
)
def test_an_incomplete_list_is_unavailable(tmp_path, metadata) -> None:
    def runner(args, **kw):
        if args[:2] == ["docker", "port"]:
            return subprocess.CompletedProcess(args, 0, "127.0.0.1:43210\n", "")
        out = {"kind": "List", "items": [], "metadata": metadata}
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")

    reader, _ = _reader(tmp_path, runner)
    assert reader.list("ReplicaSet", "testbed", 10).outcome is ReadOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "", "Error from server (InternalError)"),
        subprocess.CompletedProcess([], 0, "not json", ""),
        subprocess.CompletedProcess([], 0, "[1]", ""),
        subprocess.TimeoutExpired(["kubectl"], 10),
    ],
    ids=["error", "garbage", "not-an-object", "timeout"],
)
def test_a_failed_read_is_unavailable(tmp_path, result) -> None:
    def runner(args, **kw):
        if args[:2] == ["docker", "port"]:
            return subprocess.CompletedProcess(args, 0, "127.0.0.1:43210\n", "")
        if isinstance(result, Exception):
            raise result
        return result

    reader, _ = _reader(tmp_path, runner)
    assert reader.get("Service", "orders-svc", "testbed", 10).outcome is ReadOutcome.UNAVAILABLE


def test_a_missing_reader_credential_is_unavailable(tmp_path) -> None:
    cfg = scratch_cfg(tmp_path)  # nothing issued
    reader = KubernetesReaderTransport(cfg, runner=_never)
    assert reader.get("Deployment", "orders", "testbed", 10).outcome is ReadOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.get("Secret", "postgres-credentials", "testbed", 10),
        lambda r: r.get("Deployment", "orders", "safeops-system", 10),
        lambda r: r.get("Deployment", "--all", "testbed", 10),
        lambda r: r.list("Pod", "testbed", 10),
        lambda r: r.list("ReplicaSet", "default", 10),
    ],
)
def test_the_reader_refuses_reads_the_executor_never_needs(tmp_path, call) -> None:
    reader, run = _reader(tmp_path)
    with pytest.raises(GuardError):
        call(reader)
    assert run.calls == []


def test_the_reader_transport_uses_the_reader_phase(tmp_path) -> None:
    reader, _ = _reader(tmp_path)
    reader.get("Deployment", "orders", "testbed", 10)
    assert reader._kubectl.phase is Phase.READER
