"""Context guard, credentials, and the workload proxy capability (decisions 0006, 0009)."""

from __future__ import annotations

import ast
import base64
import json
import subprocess
from pathlib import Path

import pytest

from safeops.cluster import (
    REPO_ROOT,
    ClusterConfig,
    CredentialError,
    GuardError,
    Kubectl,
    Phase,
    WorkloadProxy,
    check_object,
    decode_token_times,
    harness_credential,
    kubeconfig_for,
    load_cluster_config,
    parse_cluster_config,
)

CFG = load_cluster_config()


def _cfg(tmp_path: Path) -> ClusterConfig:
    import yaml

    data = yaml.safe_load((REPO_ROOT / "config" / "cluster.yaml").read_text())
    return parse_cluster_config(data, root=tmp_path)


class Recorder:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def __call__(self, args, input=None, **kwargs):
        self.calls.append(list(args))
        self.stdins.append(input)
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


# --- config ------------------------------------------------------------------------------


def test_config_identity() -> None:
    assert CFG.kube_context == f"kind-{CFG.cluster_name}" == "kind-safeops"
    assert CFG.namespace == "testbed"
    assert CFG.chaos_namespace == "chaos-mesh"
    assert CFG.kubeconfig_path == REPO_ROOT / ".safeops" / "kubeconfig"
    assert CFG.harness_kubeconfig_path.parent == REPO_ROOT / ".safeops"


def test_config_rejects_context_that_is_not_kinds_name(tmp_path: Path) -> None:
    import yaml

    data = yaml.safe_load((REPO_ROOT / "config" / "cluster.yaml").read_text())
    data["kube_context"] = "minikube"
    with pytest.raises(ValueError):
        parse_cluster_config(data, root=tmp_path)


def test_safeops_state_dir_is_gitignored() -> None:
    assert ".safeops/" in (REPO_ROOT / ".gitignore").read_text().splitlines()


# --- object guard --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ns",
    [
        "chaos-mesh",
        "safeops-system",
        "safeops-workload",
        "safeops-monitoring",
        "kube-system",
        "default",
    ],
)
def test_runtime_refuses_every_namespace_but_testbed(ns: str) -> None:
    with pytest.raises(GuardError):
        check_object(CFG, Phase.RUNTIME, kind="ConfigMap", name="x", namespace=ns)


def test_runtime_allows_testbed() -> None:
    check_object(CFG, Phase.RUNTIME, kind="Deployment", name="orders", namespace="testbed")


def test_runtime_refuses_cluster_scoped_even_if_in_bootstrap_scope() -> None:
    scope = frozenset({("Namespace", "testbed")})
    with pytest.raises(GuardError):
        check_object(
            CFG,
            Phase.RUNTIME,
            kind="Namespace",
            name="testbed",
            namespace=None,
            cluster_scope=scope,
        )


def test_bootstrap_allows_its_namespaces_only() -> None:
    for ns in CFG.bootstrap_namespaces:
        check_object(CFG, Phase.BOOTSTRAP, kind="ConfigMap", name="x", namespace=ns)
    for ns in ("kube-system", "default", "minikube-stuff"):
        with pytest.raises(GuardError):
            check_object(CFG, Phase.BOOTSTRAP, kind="ConfigMap", name="x", namespace=ns)


def test_bootstrap_allows_exactly_the_recorded_cluster_scope() -> None:
    scope = frozenset({("ClusterRole", "prometheus-server")})
    check_object(
        CFG,
        Phase.BOOTSTRAP,
        kind="ClusterRole",
        name="prometheus-server",
        namespace=None,
        cluster_scope=scope,
    )
    for kind, name in [
        ("ClusterRole", "cluster-admin"),
        ("ClusterRoleBinding", "prometheus-server"),
        ("Namespace", "kube-system"),
    ]:
        with pytest.raises(GuardError):
            check_object(
                CFG, Phase.BOOTSTRAP, kind=kind, name=name, namespace=None, cluster_scope=scope
            )


def test_recorded_bootstrap_scope_has_no_cluster_admin_grants() -> None:
    import yaml

    scope = yaml.safe_load((REPO_ROOT / "config" / "bootstrap_scope.yaml").read_text())
    names = [o["name"] for c in scope["charts"].values() for o in c["cluster_scoped"]]
    names += [o["name"] for o in scope["safeops_manifests"]["cluster_scoped"]]
    assert "cluster-admin" not in names
    namespaces = {
        o["name"] for o in scope["safeops_manifests"]["cluster_scoped"] if o["kind"] == "Namespace"
    }
    assert namespaces == set(CFG.bootstrap_namespaces)


# --- the bootstrap phase is reachable only from the bootstrap module ------------------------


def _bootstrap_phase_uses(source: str) -> bool:
    """Any way of reaching the bootstrap phase: Phase.BOOTSTRAP, Phase("bootstrap"),
    Phase["BOOTSTRAP"], or the literal "bootstrap" passed to Phase."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "BOOTSTRAP":
            return True
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) == "Phase"
        ):
            return True
        if isinstance(node, ast.Subscript) and getattr(node.value, "id", None) == "Phase":
            return True
    return False


def test_only_bootstrap_module_uses_bootstrap_phase() -> None:
    users = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "safeops").rglob("*.py")
        if _bootstrap_phase_uses(path.read_text())
    }
    assert users == {"safeops/bootstrap.py", "safeops/cluster.py"}


@pytest.mark.parametrize(
    "source",
    [
        "x = Phase.BOOTSTRAP",
        'x = Phase("bootstrap")',
        'x = Phase["BOOTSTRAP"]',
        'x = cluster.Phase("bootstrap")',
    ],
)
def test_bootstrap_phase_detector_catches_every_spelling(source: str) -> None:
    """Milestone 2 review, T1: the detector itself must be able to fail."""
    assert _bootstrap_phase_uses(source)
    assert not _bootstrap_phase_uses("x = Phase.RUNTIME")


# --- kubectl wrapper ------------------------------------------------------------------------


def test_every_command_passes_kubeconfig_and_context_explicitly(tmp_path: Path) -> None:
    rec = Recorder(stdout="{}")
    k = Kubectl(
        _cfg(tmp_path), Phase.RUNTIME, runner=rec, kubeconfig=tmp_path / "kc", verify_identity=False
    )
    k.read("get", "pods", "-n", "testbed")
    assert rec.calls[0][:5] == [
        "kubectl",
        "--kubeconfig",
        str(tmp_path / "kc"),
        "--context",
        "kind-safeops",
    ]
    assert "current-context" not in " ".join(rec.calls[0])


@pytest.mark.parametrize(
    "args",
    [
        ("delete", "pod", "x"),
        ("apply", "-f", "-"),
        ("patch", "x"),
        ("exec", "pod"),
        ("rollout", "restart", "deploy/x"),
        ("config", "use-context", "minikube"),
    ],
)
def test_read_refuses_mutating_verbs(tmp_path: Path, args) -> None:
    k = Kubectl(
        _cfg(tmp_path),
        Phase.RUNTIME,
        runner=Recorder(),
        kubeconfig=tmp_path / "kc",
        verify_identity=False,
    )
    with pytest.raises(GuardError):
        k.read(*args)


def test_apply_checks_every_object_before_running(tmp_path: Path) -> None:
    rec = Recorder()
    k = Kubectl(
        _cfg(tmp_path), Phase.RUNTIME, runner=rec, kubeconfig=tmp_path / "kc", verify_identity=False
    )
    objs = [
        {"kind": "ConfigMap", "metadata": {"name": "a", "namespace": "testbed"}},
        {"kind": "ConfigMap", "metadata": {"name": "b", "namespace": "kube-system"}},
    ]
    with pytest.raises(GuardError):
        k.apply(objs)
    assert not [
        c for c in rec.calls if "apply" in c
    ]  # nothing applied, not even the allowed object


def test_apply_refuses_objects_without_namespace(tmp_path: Path) -> None:
    k = Kubectl(
        _cfg(tmp_path),
        Phase.RUNTIME,
        runner=Recorder(),
        kubeconfig=tmp_path / "kc",
        verify_identity=False,
    )
    with pytest.raises(GuardError):
        k.apply([{"kind": "ConfigMap", "metadata": {"name": "a"}}])


def test_runtime_raw_calls_only_reach_the_workload_proxy(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    k = Kubectl(
        cfg,
        Phase.RUNTIME,
        runner=Recorder(stdout="{}"),
        kubeconfig=tmp_path / "kc",
        verify_identity=False,
    )
    ok = f"/api/v1/namespaces/{cfg.workload_namespace}/services/{cfg.workload_service}/proxy/stats"
    k.raw("GET", ok)
    for bad in [
        "/api/v1/namespaces/testbed/secrets",
        f"/api/v1/namespaces/{cfg.workload_namespace}/services/other/proxy/stats",
        ok.replace("/proxy/stats", "/proxy/../../secrets"),
    ]:
        with pytest.raises(GuardError):
            k.raw("GET", bad)


def test_unauthorized_is_a_credential_error_not_a_retry(tmp_path: Path) -> None:
    rec = Recorder(returncode=1, stderr="error: You must be logged in to the server (Unauthorized)")
    k = Kubectl(
        _cfg(tmp_path), Phase.RUNTIME, runner=rec, kubeconfig=tmp_path / "kc", verify_identity=False
    )
    with pytest.raises(CredentialError, match="never falls back"):
        k.read("get", "pods")
    assert len(rec.calls) == 1


# --- credentials: runtime never falls back to admin ----------------------------------------


def test_runtime_without_harness_credential_fails_and_never_uses_admin(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.kubeconfig_path.parent.mkdir(parents=True)
    cfg.kubeconfig_path.write_text("admin")  # admin exists; it must not be used
    with pytest.raises(CredentialError, match="never falls back"):
        kubeconfig_for(cfg, Phase.RUNTIME)
    with pytest.raises(CredentialError):
        WorkloadProxy(cfg).stats()


def test_expired_harness_credential_fails(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.harness_kubeconfig_path.parent.mkdir(parents=True)
    cfg.harness_kubeconfig_path.write_text("x")
    cfg.harness_token_meta_path.write_text(json.dumps({"expires_at": 1000}))
    with pytest.raises(CredentialError, match="expired"):
        harness_credential(cfg, now=lambda: 1000)
    assert harness_credential(cfg, now=lambda: 999) == cfg.harness_kubeconfig_path


def test_bootstrap_uses_admin_kubeconfig(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.kubeconfig_path.parent.mkdir(parents=True)
    cfg.kubeconfig_path.write_text("admin")
    assert kubeconfig_for(cfg, Phase.BOOTSTRAP) == cfg.kubeconfig_path


def test_runtime_cannot_issue_tokens(tmp_path: Path) -> None:
    k = Kubectl(
        _cfg(tmp_path),
        Phase.RUNTIME,
        runner=Recorder(),
        kubeconfig=tmp_path / "kc",
        verify_identity=False,
    )
    with pytest.raises(GuardError):
        k.create_token("experiment-controller", "safeops-system", 3600)


def _jwt(claims: dict) -> str:
    enc = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{enc}.signature"


def test_token_lifetime_is_read_from_the_issued_token() -> None:
    assert decode_token_times(_jwt({"iat": 100, "exp": 3700})) == (100, 3700)
    with pytest.raises(CredentialError):
        decode_token_times("not-a-jwt")


def test_short_issued_token_fails_bootstrap(tmp_path: Path, monkeypatch) -> None:
    from safeops import bootstrap

    cfg = _cfg(tmp_path)

    class FakeKubectl:
        def create_token(self, *args):
            return _jwt({"iat": 0, "exp": cfg.harness_session_s - 1})  # capped by the API server

    with pytest.raises(CredentialError, match="Refusing"):
        bootstrap.issue_harness_credential(cfg, FakeKubectl())
    assert not cfg.harness_kubeconfig_path.exists()


# --- workload proxy capability ------------------------------------------------------------


def test_proxy_path_uses_plain_service_name_for_rbac_resource_names() -> None:
    p = WorkloadProxy(CFG).path(CFG.workload_namespace, CFG.workload_service, "stats", "?window=w1")
    assert (
        p == "/api/v1/namespaces/safeops-workload/services/workload-generator/proxy/stats?window=w1"
    )
    assert (
        WorkloadProxy(CFG)
        .path(CFG.workload_namespace, CFG.workload_service, "control")
        .endswith("/proxy/control")
    )


@pytest.mark.parametrize(
    ("namespace", "service", "operation"),
    [
        ("testbed", "workload-generator", "stats"),
        ("safeops-workload", "prometheus-server", "stats"),
        ("safeops-workload", "workload-generator", "metrics"),
        ("safeops-workload", "workload-generator", "exec"),
    ],
)
def test_proxy_capability_refuses_anything_else(namespace, service, operation) -> None:
    with pytest.raises(GuardError):
        WorkloadProxy(CFG).path(namespace, service, operation)


@pytest.mark.parametrize("query", ["?x=/../secrets", "window=w1", "?a#b"])
def test_proxy_capability_refuses_odd_queries(query: str) -> None:
    with pytest.raises(GuardError):
        WorkloadProxy(CFG).path(CFG.workload_namespace, CFG.workload_service, "stats", query)


def test_proxy_methods_are_get_for_stats_and_put_for_control(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rec = Recorder(stdout='{"ok": true}')
    proxy = WorkloadProxy(
        cfg,
        Kubectl(cfg, Phase.RUNTIME, runner=rec, kubeconfig=tmp_path / "kc", verify_identity=False),
    )
    proxy.stats("w3")
    proxy.control({"action": "stop"})
    assert rec.calls[0][5:7] == ["get", "--raw"] and rec.calls[0][7].endswith("/stats?window=w3")
    assert rec.calls[1][5:7] == ["replace", "--raw"] and rec.calls[1][7].endswith("/control")
    assert json.loads(rec.stdins[1]) == {"action": "stop"}
    with pytest.raises(GuardError):
        proxy.stats("../etc")
