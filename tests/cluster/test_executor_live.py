"""Milestone 8B-2, live (decision 0025 D§11-D§14, M8B-2 amendment and STOP resolution): facts
a fake can't establish. Run with `make cluster-test` after `make cluster-up`.

- The STOP resolution: the executor's own token is refused a GET of an allowed Deployment
  while the production writer's direct HTTPS PATCH of the same Deployment succeeds, through
  the verified authority, the pinned CA and hostname verification. No kubectl patch, no GET.

- safeops-executor's three RBAC proof layers, kept apart: the production client guard; the
  admin's impersonation of its Role and RoleBinding; and its own token against the API server
  (server-side dry runs, bypassing the production guard in this test code only).
- Every frozen action works through the real executor with patch-only RBAC: the six
  operations, exact Service selector replacement, the request > limit 422, an ambiguous write
  reconciled by safeops-reader, the provisional rollback template equality against real server
  defaulting, and the quantity forms the API server returns.
- The audit trail: run ids, files and event order; no secret in it.

Each test starts from and ends at a clean, reset testbed (EnvironmentManager, the harness
path), so no test depends on another. Audit roots are temporary.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import uuid
from pathlib import Path

import pytest

from safeops.bootstrap import _can_i, can_i_answer, executor_rbac_expectations
from safeops.cluster import GuardError, Kubectl, Phase, load_cluster_config
from safeops.domain import ActionTarget, RecommendedAction
from safeops.enums import ActionType, ExecutionStatus
from safeops.environment.manager import EnvironmentManager
from safeops.executor import quantity
from safeops.executor.audit import AuditPersistenceError, JsonlExecutorAudit
from safeops.executor.executor import RESTART_ANNOTATION, REVISION_ANNOTATION, ActionExecutor
from safeops.executor.kubernetes import KubernetesReaderTransport, KubernetesWriterTransport
from safeops.executor.transport import ReadOutcome, WriteClass, WriteReply, canonical_json
from safeops.kube_patch import (
    MAX_EXECUTOR_HTTP_BODY_BYTES,
    ExecutorPatchClient,
    https_connection,
)
from safeops.policy import load_action_semantics
from safeops.topology import load_topology

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFEOPS_CLUSTER_TESTS") != "1",
    reason="needs a live cluster (make cluster-test)",
)

CFG = load_cluster_config()
NS = CFG.namespace
S = ExecutionStatus
EXECUTOR = [
    "kubectl",
    "--kubeconfig",
    str(CFG.executor_kubeconfig_path),
    "--context",
    CFG.kube_context,
]
LABEL = '{"metadata":{"labels":{"safeops-probe":"denied"}}}'
ADMIN = ["kubectl", "--kubeconfig", str(CFG.kubeconfig_path), "--context", CFG.kube_context]


def _kubectl(base: list[str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        base + list(args), capture_output=True, text=True, timeout=120, check=False
    )


def _get(kind: str, name: str) -> dict:
    """The object as the admin sees it (test-only observation, outside SafeOps)."""
    out = _kubectl(ADMIN, "get", kind, name, "-n", NS, "-o", "json")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def act(type_: str, kind: str, name: str, params: dict) -> RecommendedAction:
    return RecommendedAction(
        type=ActionType(type_), target=ActionTarget(kind=kind, ns=NS, name=name), params=params
    )


@pytest.fixture(scope="module")
def env() -> EnvironmentManager:
    e = EnvironmentManager()
    if not e.verify_clean_state().clean:
        e.reset_environment()
    assert e.verify_clean_state().clean, "precondition: start from a clean testbed"
    return e


@pytest.fixture
def clean(env):
    """Reset and verify after each test: no live mutation outlives it."""
    yield env
    env.reset_environment()
    result = env.verify_clean_state()
    assert result.clean, result.problems[:5]


class RecordingConnect:
    """The production connection factory, recording what the writer sends: the authority,
    the TLS settings, each request line and its non-secret headers. The Authorization header
    is recorded only as present; its value is never kept."""

    def __init__(self) -> None:
        self.connections: list[dict] = []

    def __call__(self, host, port, *, context, timeout):
        conn = https_connection(host, port, context=context, timeout=timeout)
        seen = {
            "authority": (host, port),
            "verify_mode": context.verify_mode,
            "check_hostname": context.check_hostname,
            "ca_count": len(context.get_ca_certs()),
            "timeout": timeout,
            "requests": [],
            "headers": {},
        }
        self.connections.append(seen)
        putrequest, putheader = conn.putrequest, conn.putheader

        def recording_putrequest(method, url, *a, **k):
            seen["requests"].append((method, url))
            return putrequest(method, url, *a, **k)

        def recording_putheader(name, *values):
            seen["headers"][name] = "<present>" if name.lower() == "authorization" else values
            return putheader(name, *values)

        conn.putrequest, conn.putheader = recording_putrequest, recording_putheader
        return conn

    @property
    def requests(self) -> list[tuple[str, str]]:
        return [r for c in self.connections for r in c["requests"]]


class Wired:
    """The production executor wiring, with a temporary audit root and a fresh run id."""

    def __init__(self, tmp_path: Path, *, writer_wrap=None, sink=None) -> None:
        self.sink = sink or JsonlExecutorAudit(tmp_path / "audit")
        self.http = RecordingConnect()
        real = KubernetesWriterTransport(CFG, self.sink, connect=self.http)
        self.writer = writer_wrap(real) if writer_wrap else real
        self.reader = KubernetesReaderTransport(CFG)
        self.executor = ActionExecutor(
            semantics=load_action_semantics(),
            topology=load_topology(),
            namespace=NS,
            reader=self.reader,
            writer=self.writer,
            audit=self.sink,
        )
        self.run_id = str(uuid.uuid4())
        self.executor.bind_run(self.run_id)

    def apply(self, action):
        return self.executor.apply(action)

    def events(self) -> list[dict]:
        root = getattr(self.sink, "root", None) or self.sink.inner.root
        path = Path(root) / f"{self.run_id}.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()]


class Capturing:
    """The real writer, keeping every reply (to see the API server's returned objects)."""

    def __init__(self, inner, hide_success: bool = False) -> None:
        self.inner, self.audit, self.hide = inner, inner.audit, hide_success
        self.replies: list[WriteReply] = []

    def preflight(self, requests):
        return self.inner.preflight(requests)

    def write(self, request):
        reply = self.inner.write(request)
        self.replies.append(reply)
        if self.hide and reply.semantic_class is WriteClass.SUCCESS:
            # the write really happened; its response is "lost"
            return WriteReply(WriteClass.AMBIGUOUS, diagnostic="response hidden by the test")
        return reply


# --- RBAC: three separate proof layers ----------------------------------------------------------


def test_layer1_the_production_client_refuses_reads_and_other_writes_live() -> None:
    k = Kubectl(CFG, Phase.EXECUTOR)
    k.verify_identity()  # the real executor credential, against the real cluster
    for call in (
        lambda: k.read("get", "deployments", "-n", NS),
        lambda: k.read("auth", "can-i", "patch", "services"),
        lambda: k.cluster_scoped_kinds(),
        lambda: k.raw("GET", f"/api/v1/namespaces/{NS}/configmaps"),
        lambda: k.delete("configmap", "ConfigMap", "orders-config", NS),
    ):
        with pytest.raises(GuardError):
            call()


def test_layer2_admin_impersonation_proves_the_role_and_binding() -> None:
    wrong = [
        e
        for e in executor_rbac_expectations(CFG)
        if _can_i(CFG, e[0], e[1], e[2], e[3], service_account="safeops-executor") != e[4]
    ]
    assert not wrong, wrong


def test_layer3_the_executor_token_itself_has_exactly_the_ceiling() -> None:
    wrong = []
    for verb, resource, sub, ns, want in executor_rbac_expectations(CFG):
        args = ["auth", "can-i", verb, resource, *([f"--subresource={sub}"] if sub else [])]
        if can_i_answer(_kubectl(EXECUTOR, *args, "-n", ns)) != want:
            wrong.append((verb, resource, sub, ns, want))
    assert not wrong, wrong


@pytest.mark.parametrize(
    "args",
    [
        ("get", "deployment", "orders", "-n", NS),
        ("get", "configmap", "db-pool-config", "-n", NS),
        ("get", "replicasets", "-n", NS),
        ("delete", "deployment", "orders", "-n", NS, "--dry-run=server"),
        ("delete", "configmap", "orders-config", "-n", NS, "--dry-run=server"),
        ("scale", "deployment", "orders", "--replicas=3", "-n", NS, "--dry-run=server"),
        (
            "patch",
            "statefulset",
            "postgres",
            "-n",
            NS,
            "--dry-run=server",
            "--type=merge",
            "-p",
            LABEL,
        ),
        (
            "patch",
            "secret",
            "postgres-credentials",
            "-n",
            NS,
            "--dry-run=server",
            "--type=merge",
            "-p",
            LABEL,
        ),
        (
            "patch",
            "configmap",
            "kube-root-ca.crt",
            "-n",
            NS,
            "--dry-run=server",
            "--type=merge",
            "-p",
            LABEL,
        ),
        (
            "patch",
            "deployment",
            "orders",
            "-n",
            "kube-system",
            "--dry-run=server",
            "--type=merge",
            "-p",
            LABEL,
        ),
        (
            "patch",
            "deployment",
            "orders",
            "-n",
            NS,
            "--subresource=scale",
            "--dry-run=server",
            "--type=merge",
            "-p",
            '{"spec":{"replicas":3}}',
        ),
    ],
)
def test_the_api_server_refuses_the_executor_s_reads_and_out_of_ceiling_writes(args) -> None:
    out = _kubectl(EXECUTOR, *args)
    assert out.returncode != 0
    assert "Forbidden" in out.stderr or "forbidden" in out.stderr, out.stderr


def test_update_is_denied_to_the_executor_token() -> None:
    """`kubectl replace` (an update) is refused (server-side dry run, nothing kept)."""
    obj = _get("configmap", "orders-config")
    replaced = subprocess.run(
        EXECUTOR + ["replace", "--dry-run=server", "-f", "-"],
        input=json.dumps(obj),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert replaced.returncode != 0 and "forbidden" in replaced.stderr.lower()
    print("update refusal:", replaced.stderr.strip().splitlines()[-1])


def test_kubectl_patch_itself_is_blocked_by_its_own_get() -> None:
    """The original STOP, kept as evidence: kubectl patch GETs the object first, the GET is
    403, and no PATCH is sent (server-side dry run: nothing could be kept anyway)."""
    out = _kubectl(
        EXECUTOR,
        "patch",
        "deployment",
        "orders",
        "-n",
        NS,
        "--type=merge",
        "-p",
        LABEL,
        "--dry-run=server",
        "-v=6",
    )
    assert out.returncode != 0
    responses = [ln for ln in out.stderr.splitlines() if "Response" in ln and "verb=" in ln]
    assert any('verb="GET"' in ln and "403" in ln for ln in responses), responses
    assert not any('verb="PATCH"' in ln for ln in responses), responses


def test_the_executor_get_is_denied_while_its_direct_patch_succeeds(clean, tmp_path) -> None:
    """The STOP resolution, live: the same credential, the same allowed Deployment."""
    denied = _kubectl(EXECUTOR, "get", "deployment", "orders", "-n", NS)
    assert denied.returncode != 0 and "cannot get resource" in denied.stderr, denied.stderr
    w = Wired(tmp_path, writer_wrap=Capturing)
    result = w.apply(act("scale_deployment", "Deployment", "orders", {"replicas": 3}))
    assert result.status is S.APPLIED, result
    assert _get("deployment", "orders")["spec"]["replicas"] == 3
    path = f"/apis/apps/v1/namespaces/{NS}/deployments/orders"
    assert w.http.requests == [("PATCH", path)]  # one PATCH, zero executor GET, no kubectl
    (conn,) = w.http.connections
    host, port = conn["authority"]
    server = yaml_server(CFG.executor_kubeconfig_path)
    assert f"https://{host}:{port}" == server  # the verified authority, exactly
    assert conn["verify_mode"] is ssl.CERT_REQUIRED and conn["check_hostname"] is True
    assert conn["ca_count"] == 1 and conn["timeout"] == 20
    assert conn["headers"]["Content-Type"] == ("application/merge-patch+json",)
    assert conn["headers"]["Authorization"] == "<present>"
    print("direct PATCH:", path, "authority", conn["authority"], "TLS verified, pinned CA")


def yaml_server(path: Path) -> str:
    import yaml

    kc = yaml.safe_load(path.read_text())
    return kc["clusters"][0]["cluster"]["server"]  # the server only; the token is never read


# --- the six frozen operations, live -----------------------------------------------------------


PATHS = {
    "Deployment": "/apis/apps/v1/namespaces/{ns}/deployments/{name}",
    "Service": "/api/v1/namespaces/{ns}/services/{name}",
    "ConfigMap": "/api/v1/namespaces/{ns}/configmaps/{name}",
}


def _one_patch_per_step(w: Wired, steps: list[tuple[str, str]]) -> None:
    """Audit: before_write/after_write per step, one final; HTTP: exactly one PATCH per step,
    at its exact path, and nothing else (no GET, no retry)."""
    events = w.events()
    kinds = [(e["event"], e.get("name")) for e in events if e["event"] != "read"]
    expected = []
    for _, name in steps:
        expected += [("before_write", name), ("after_write", name)]
    assert kinds == expected + [("final", None)]
    assert {e["run_id"] for e in events} == {w.run_id}
    paths = [("PATCH", PATHS[kind].format(ns=NS, name=name)) for kind, name in steps]
    assert w.http.requests == paths, w.http.requests
    print("requests:", w.http.requests)


def test_restart_is_applied_with_its_exact_annotation(clean, tmp_path) -> None:
    w = Wired(tmp_path, writer_wrap=Capturing)
    result = w.apply(act("restart_deployment", "Deployment", "orders", {}))
    assert result.status is S.APPLIED, result
    events = w.events()
    _one_patch_per_step(w, [("Deployment", "orders")])
    sent = events[0]["patch"]["spec"]["template"]["metadata"]["annotations"][RESTART_ANNOTATION]
    live = _get("deployment", "orders")["spec"]["template"]["metadata"]["annotations"]
    assert live[RESTART_ANNOTATION] == sent
    assert events[0]["identity"] == "system:serviceaccount:safeops-system:safeops-executor"
    reply = w.writer.replies[0]
    assert reply.semantic_class is WriteClass.SUCCESS
    obj = reply.returned_object
    assert (obj["kind"], obj["metadata"]["name"], obj["metadata"]["namespace"]) == (
        "Deployment",
        "orders",
        NS,
    )
    assert {e["run_id"] for e in events} == {w.run_id}


def test_scale_patches_replicas_on_the_deployment_itself(clean, tmp_path) -> None:
    w = Wired(tmp_path)
    result = w.apply(act("scale_deployment", "Deployment", "orders", {"replicas": 3}))
    assert result.status is S.APPLIED, result
    assert _get("deployment", "orders")["spec"]["replicas"] == 3
    events = w.events()
    _one_patch_per_step(w, [("Deployment", "orders")])
    assert events[0]["patch"] == {"spec": {"replicas": 3}}  # not the scale subresource


def test_the_configmap_composite_patches_then_restarts(clean, tmp_path) -> None:
    w = Wired(tmp_path)
    result = w.apply(act("patch_configmap", "ConfigMap", "db-pool-config", {"max_pool_size": 20}))
    assert result.status is S.APPLIED, result
    assert [(s.operation.value, s.status) for s in result.steps] == [
        ("patch_configmap", "applied"),
        ("restart_deployment", "applied"),
    ]
    assert _get("configmap", "db-pool-config")["data"]["max_pool_size"] == "20"
    _one_patch_per_step(w, [("ConfigMap", "db-pool-config"), ("Deployment", "orders")])


def test_a_service_selector_is_replaced_exactly_no_stale_key(clean, tmp_path) -> None:
    w = Wired(tmp_path, writer_wrap=Capturing)
    wide = {"app": "orders", "safeops-probe": "one"}
    narrow = {"safeops-probe": "two"}  # fewer keys, and a different key
    print("selector before:", _get("service", "orders-svc")["spec"]["selector"])
    first = w.apply(act("patch_service", "Service", "orders-svc", {"selector": wide}))
    assert first.status is S.APPLIED, first
    assert _get("service", "orders-svc")["spec"]["selector"] == wide
    second = w.apply(act("patch_service", "Service", "orders-svc", {"selector": narrow}))
    assert second.status is S.APPLIED, second
    assert _get("service", "orders-svc")["spec"]["selector"] == narrow  # "app" did not survive
    print(
        "selector requested:", narrow, "after:", _get("service", "orders-svc")["spec"]["selector"]
    )
    path = PATHS["Service"].format(ns=NS, name="orders-svc")
    assert w.http.requests == [("PATCH", path), ("PATCH", path)]
    obj = w.writer.replies[-1].returned_object
    assert (obj["kind"], obj["metadata"]["name"], obj["metadata"]["namespace"]) == (
        "Service",
        "orders-svc",
        NS,
    )


def test_resources_are_patched_on_the_existing_container_and_read_back_exactly(
    clean, tmp_path
) -> None:
    w = Wired(tmp_path, writer_wrap=Capturing)
    params = {
        "container": "orders",
        "requests": {"cpu_millis": 150, "memory_mib": 1024},
        "limits": {"cpu_millis": 1500, "memory_mib": 2048},
    }
    before = _get("deployment", "orders")["spec"]["template"]["spec"]["containers"]
    result = w.apply(act("patch_deployment_resources", "Deployment", "orders", params))
    assert result.status is S.APPLIED, result
    _one_patch_per_step(w, [("Deployment", "orders")])
    after = _get("deployment", "orders")["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in after] == [c["name"] for c in before]  # no new container
    for b, a in zip(before, after, strict=True):
        assert {k: v for k, v in a.items() if k != "resources"} == {
            k: v for k, v in b.items() if k != "resources"
        }  # unlisted fields preserved
    events = w.events()
    assert events[0]["event"] == "read" and events[0]["purpose"] == "container_check"
    container = next(
        c
        for c in _get("deployment", "orders")["spec"]["template"]["spec"]["containers"]
        if c["name"] == "orders"
    )
    resources = container["resources"]
    print("observed quantity forms:", json.dumps(resources, sort_keys=True))
    assert quantity.cpu_is(resources["requests"]["cpu"], 150)
    assert quantity.memory_is(resources["requests"]["memory"], 1024)
    assert quantity.cpu_is(resources["limits"]["cpu"], 1500)
    assert quantity.memory_is(resources["limits"]["memory"], 2048)
    returned = w.writer.replies[0].returned_object
    assert returned["kind"] == "Deployment" and returned["metadata"]["namespace"] == NS


def test_every_quantity_form_the_server_returns_is_supported() -> None:
    for name in ("gateway", "orders", "inventory", "payment"):
        for c in _get("deployment", name)["spec"]["template"]["spec"]["containers"]:
            for section in ("requests", "limits"):
                for key, value in c.get("resources", {}).get(section, {}).items():
                    parse = quantity.parse_cpu if key == "cpu" else quantity.parse_memory
                    assert parse(value) is not None, (name, section, key, value)


def test_a_rollback_matches_real_server_defaulting(clean, tmp_path) -> None:
    """The PROVISIONAL M8B-1 comparison (O7), against the real API server: the Deployment's
    stored template equals the chosen ReplicaSet's template minus pod-template-hash."""
    before = _get("deployment", "orders")
    revision = int(before["metadata"]["annotations"][REVISION_ANNOTATION])
    w = Wired(tmp_path)
    assert w.apply(act("restart_deployment", "Deployment", "orders", {})).status is S.APPLIED
    _kubectl(ADMIN, "rollout", "status", "deployment/orders", "-n", NS, "--timeout=180s")
    result = w.apply(act("rollback_deployment", "Deployment", "orders", {"to_revision": revision}))
    assert result.status is S.APPLIED, result
    after = _get("deployment", "orders")
    assert RESTART_ANNOTATION not in (
        after["spec"]["template"]["metadata"].get("annotations") or {}
    )
    reads = [e for e in w.events() if e["event"] == "read"]
    assert [r["purpose"] for r in reads] == ["rollback_history", "rollback_history"]
    path = PATHS["Deployment"].format(ns=NS, name="orders")
    assert w.http.requests == [("PATCH", path), ("PATCH", path)]  # restart, then rollback


def test_request_above_limit_is_a_real_422_with_no_mutation(clean, tmp_path) -> None:
    before = _get("deployment", "orders")
    w = Wired(tmp_path, writer_wrap=Capturing)
    params = {
        "container": "orders",
        "requests": {"memory_mib": 2048},
        "limits": {"memory_mib": 1024},
    }
    result = w.apply(act("patch_deployment_resources", "Deployment", "orders", params))
    assert result.status is S.REJECTED_INVALID_ACTION, result
    assert [(s.operation.value, s.status) for s in result.steps] == [
        ("patch_deployment_resources", "failed")
    ]
    reply = w.writer.replies[0]
    print("live 422:", reply.diagnostic)
    assert reply.semantic_class is WriteClass.INVALID_ACTION and "HTTP 422" in reply.diagnostic
    assert w.http.requests == [("PATCH", PATHS["Deployment"].format(ns=NS, name="orders"))]
    # The same rejected body once more, straight through the primitive, to see the exchange
    # (a 422 changes nothing, so this second request is harmless).
    (sent,) = [e for e in w.events() if e["event"] == "before_write"]
    exchange = ExecutorPatchClient(CFG).patch(
        kind="Deployment",
        name="orders",
        namespace=NS,
        patch_type=sent["patch_type"],
        body_json=canonical_json(sent["patch"]),
        timeout_s=20,
    )
    status = json.loads(exchange.body_prefix)
    print(
        "422 shape:",
        {
            "http_status": exchange.status,
            "bytes": len(exchange.body_prefix),
            "body_truncated": exchange.body_truncated,
            "kind": status["kind"],
            "reason": status["reason"],
            "code": status["code"],
        },
    )
    assert exchange.status == 422 and not exchange.body_truncated
    assert len(exchange.body_prefix) < MAX_EXECUTOR_HTTP_BODY_BYTES
    assert (status["kind"], status["reason"], status["code"]) == ("Status", "Invalid", 422)
    after = _get("deployment", "orders")
    assert after["metadata"]["generation"] == before["metadata"]["generation"]
    assert after["spec"]["template"] == before["spec"]["template"]


def test_an_ambiguous_write_is_reconciled_by_the_reader_without_a_second_write(
    clean, tmp_path
) -> None:
    w = Wired(tmp_path, writer_wrap=lambda real: Capturing(real, hide_success=True))
    result = w.apply(act("scale_deployment", "Deployment", "orders", {"replicas": 4}))
    assert result.status is S.APPLIED, result
    assert len(w.writer.replies) == 1  # one real write, never retried
    assert w.http.requests == [("PATCH", PATHS["Deployment"].format(ns=NS, name="orders"))]
    events = w.events()
    assert [(e["event"], e.get("purpose")) for e in events] == [
        ("before_write", None),
        ("after_write", None),
        ("read", "reconciliation"),
        ("final", None),
    ]
    assert events[2]["reconciliation"]["outcome"] == "confirmed"
    assert events[2]["reconciliation"]["identity"] == ["Deployment", "orders", NS]


def test_an_after_write_audit_failure_keeps_the_real_write(clean, tmp_path) -> None:
    class FailingAfter:
        """The real sink, except after_write fails (every Protocol method defined)."""

        def __init__(self, inner):
            self.inner = inner

        def bind_run(self, run_id):
            self.inner.bind_run(run_id)

        def require_bound(self):
            self.inner.require_bound()

        def before_write(self, request, identity):
            self.inner.before_write(request, identity)

        def read(self, **fields):
            self.inner.read(**fields)

        def final(self, result):
            self.inner.final(result)

        def after_write(self, request, reply):
            raise AuditPersistenceError("injected after_write failure (test only)")

    sink = FailingAfter(JsonlExecutorAudit(tmp_path / "audit"))
    w = Wired(tmp_path, writer_wrap=Capturing, sink=sink)
    result = w.apply(act("scale_deployment", "Deployment", "orders", {"replicas": 3}))
    assert result.status is S.APPLIED, result
    assert len(w.writer.replies) == 1 and len(w.http.requests) == 1
    assert _get("deployment", "orders")["spec"]["replicas"] == 3
    assert [e["event"] for e in w.events()] == ["before_write", "final"]


def test_reader_objects_and_list_items_carry_their_identity() -> None:
    reader = KubernetesReaderTransport(CFG)
    for kind, name in (
        ("Deployment", "orders"),
        ("Service", "orders-svc"),
        ("ConfigMap", "db-pool-config"),
    ):
        reply = reader.get(kind, name, NS, 10)
        assert reply.outcome is ReadOutcome.FOUND
        obj = reply.obj
        assert (obj["kind"], obj["metadata"]["name"], obj["metadata"]["namespace"]) == (
            kind,
            name,
            NS,
        )
    assert reader.get("Deployment", "no-such-deployment", NS, 10).outcome is ReadOutcome.ABSENT
    listed = reader.list("ReplicaSet", NS, 10)
    assert listed.outcome is ReadOutcome.FOUND and listed.obj["items"]
    for item in listed.obj["items"]:
        assert item["kind"] == "ReplicaSet" and item["metadata"]["namespace"] == NS
        assert item["metadata"]["name"]


def test_a_live_refusal_closes_its_audit_with_one_final(tmp_path) -> None:
    w = Wired(tmp_path)
    result = w.apply(act("scale_deployment", "Deployment", "orders", {"replicas": 500}))
    assert result.status is S.REFUSED_BY_ENVELOPE
    assert [e["event"] for e in w.events()] == ["final"]
    gone = w.apply(
        act(
            "patch_deployment_resources",
            "Deployment",
            "orders",
            {"container": "no-such-container", "limits": {"cpu_millis": 500}},
        )
    )
    assert gone.status is S.REFUSED_BY_ENVELOPE
    assert [e["event"] for e in w.events()] == ["final", "read", "final"]


def test_no_audit_file_holds_a_credential(clean, tmp_path) -> None:
    """No token (a JWT starts "eyJ"), Authorization header or bearer value reaches the trail,
    across a refusal, a real write (with kubectl's -v=6 diagnostic) and a real 422.
    The credential itself is never read here, so no failure can print it."""
    w = Wired(tmp_path)
    w.apply(act("scale_deployment", "Deployment", "orders", {"replicas": 500}))
    assert w.apply(act("scale_deployment", "Deployment", "orders", {"replicas": 3})).status is (
        S.APPLIED
    )
    w.apply(
        act(
            "patch_deployment_resources",
            "Deployment",
            "orders",
            {
                "container": "orders",
                "requests": {"memory_mib": 2048},
                "limits": {"memory_mib": 1024},
            },
        )
    )
    events = [e["event"] for e in w.events()]
    assert events.count("before_write") == 2 and events.count("final") == 3
    for path in (tmp_path / "audit").iterdir():
        text = path.read_text()
        leaked = [
            m
            for m in (
                "eyJ",
                "Bearer",
                "Authorization",
                "BEGIN CERTIFICATE",
                "certificate-authority",
            )
            if m in text
        ]
        assert leaked == []
        assert len(text) < 64 * 1024  # bounded diagnostics, never a whole response body
        for line in text.splitlines():
            event = json.loads(line)
            assert "body" not in event and "response" not in event
