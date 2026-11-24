"""Regression tests for the Milestone 2 adversarial review. Each test is labelled with the
finding it closes. Before the fix, every one of these reproduced the problem.
"""

from __future__ import annotations

import importlib.util
import io
import json
import stat
import subprocess
import sys
from email.message import Message
from pathlib import Path

import pytest

from safeops.cluster import (
    REPO_ROOT,
    CommandError,
    GuardError,
    Kubectl,
    Phase,
    parse_cluster_config,
)
from safeops.environment.workload import (
    SteadyStateError,
    SteadyStateMeasurementFailed,
    WorkloadGenerator,
    WorkloadGeneratorBusy,
    WorkloadSettings,
    check_steady_state,
)


def _cfg(tmp_path: Path):
    import yaml

    data = yaml.safe_load((REPO_ROOT / "config" / "cluster.yaml").read_text())
    return parse_cluster_config(data, root=tmp_path)


class Runner:
    """Fake subprocess.run; answers api-resources with a configurable listing."""

    def __init__(self, cluster_kinds: str = "") -> None:
        self.calls: list[list[str]] = []
        self.cluster_kinds = cluster_kinds

    def __call__(self, args, input=None, **kwargs):
        self.calls.append(list(args))
        out = self.cluster_kinds if "api-resources" in args else "{}"
        return subprocess.CompletedProcess(args, 0, out, "")


def _k(tmp_path, phase=Phase.RUNTIME, runner=None):
    return Kubectl(
        _cfg(tmp_path),
        phase,
        runner=runner or Runner(),
        kubeconfig=tmp_path / "kc",
        verify_identity=False,
    )


# --- G1-G4: read() can't override identity, reach raw paths, or mutate via subcommands --------


@pytest.mark.parametrize(
    "args",
    [
        ("get", "pods", "--context", "minikube"),  # G2
        ("get", "pods", "--context=minikube"),  # G2, = form
        ("get", "pods", "--kubeconfig", "/Users/x/.kube/config"),  # G3
        ("get", "pods", "--server", "https://elsewhere"),
        ("get", "pods", "-s", "https://elsewhere"),
        ("get", "pods", "--token", "t"),
        ("get", "pods", "--as", "system:admin"),
        ("get", "pods", "--user", "other"),
        ("get", "pods", "--cluster", "other"),
        ("get", "--raw", "/api/v1/namespaces/testbed/secrets"),  # G1
        ("get", "--raw=/api/v1/namespaces/testbed/secrets"),
    ],
)
@pytest.mark.parametrize("phase", [Phase.RUNTIME, Phase.BOOTSTRAP])
def test_read_refuses_identity_overrides_and_raw(tmp_path, args, phase) -> None:
    runner = Runner()
    with pytest.raises(GuardError):
        _k(tmp_path, phase, runner).read(*args)
    assert runner.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ("auth", "reconcile", "-f", "rbac.yaml"),
        ("auth",),
        ("auth", "whoami"),
        ("rollout", "restart", "deploy/x"),
    ],
)
def test_read_refuses_mutating_subcommands(tmp_path, args) -> None:  # G4
    with pytest.raises(GuardError):
        _k(tmp_path).read(*args)


def test_read_still_allows_can_i_and_rollout_status(tmp_path) -> None:
    k = _k(tmp_path)
    k.read("auth", "can-i", "get", "pods", "-n", "testbed")
    k.read("rollout", "status", "deploy/orders", "-n", "testbed")


# --- G5/G6: scope comes from the kind, never from metadata.namespace ------------------------


@pytest.mark.parametrize(
    "obj",
    [
        {"kind": "ClusterRole", "metadata": {"name": "evil", "namespace": "testbed"}},  # G5
        {"kind": "Namespace", "metadata": {"name": "kube-system", "namespace": "testbed"}},  # G6
        {"kind": "ClusterRoleBinding", "metadata": {"name": "x", "namespace": "testbed"}},
    ],
)
def test_runtime_apply_refuses_cluster_scoped_kinds_with_a_namespace(tmp_path, obj) -> None:
    runner = Runner()
    with pytest.raises(GuardError, match="cluster-scoped"):
        _k(tmp_path, runner=runner).apply([obj])
    assert not [c for c in runner.calls if "apply" in c]


def test_apply_classifies_live_cluster_scoped_custom_kinds(tmp_path) -> None:
    """A cluster-scoped custom kind that the built-in list doesn't know comes from the live API."""
    listing = "clusterissuers   cert-manager.io/v1   false   ClusterIssuer\n"
    runner = Runner(cluster_kinds=listing)
    obj = {"kind": "ClusterIssuer", "metadata": {"name": "x", "namespace": "testbed"}}
    with pytest.raises(GuardError, match="cluster-scoped"):
        _k(tmp_path, runner=runner).apply([obj])


def test_apply_still_applies_namespaced_testbed_objects(tmp_path) -> None:
    runner = Runner()
    _k(tmp_path, runner=runner).apply(
        [{"kind": "ConfigMap", "metadata": {"name": "a", "namespace": "testbed"}}]
    )
    applied = [c for c in runner.calls if "apply" in c]
    assert len(applied) == 1 and applied[0][1:5] == [
        "--kubeconfig",
        str(tmp_path / "kc"),
        "--context",
        "kind-safeops",
    ]


# --- B1: unknown cluster-scoped kinds can't slip past the bootstrap scope ------------------


def test_scope_records_cluster_scoped_kinds_known_only_to_the_live_api() -> None:
    from safeops.bootstrap import _cluster_scoped
    from safeops.cluster import CLUSTER_SCOPED_KINDS

    objs = [
        {"kind": "ClusterIssuer", "metadata": {"name": "rogue"}},
        {"kind": "ClusterRole", "metadata": {"name": "ok"}},
    ]
    offline = _cluster_scoped(objs, "chaos-mesh")
    live = _cluster_scoped(
        objs, "chaos-mesh", CLUSTER_SCOPED_KINDS | {"ClusterIssuer"}, frozenset({"ConfigMap"})
    )
    assert {"kind": "ClusterIssuer", "name": "rogue"} not in offline
    assert {
        "kind": "ClusterIssuer",
        "name": "rogue",
    } in live  # offline != live, so cluster-up refuses


def test_scope_refuses_kinds_unknown_to_the_live_api() -> None:
    from safeops.bootstrap import _cluster_scoped

    with pytest.raises(GuardError, match="unknown"):
        _cluster_scoped(
            [{"kind": "Mystery", "metadata": {"name": "m"}}],
            "chaos-mesh",
            namespaced_kinds=frozenset({"ConfigMap"}),
        )


def test_scope_uses_crd_scope_from_the_same_render() -> None:
    from safeops.bootstrap import _cluster_scoped

    crd = {
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "widgets.example.io"},
        "spec": {"names": {"kind": "Widget"}, "scope": "Cluster"},
    }
    widget = {"kind": "Widget", "metadata": {"name": "w"}}
    assert {"kind": "Widget", "name": "w"} in _cluster_scoped(
        [crd, widget], "chaos-mesh", namespaced_kinds=frozenset()
    )


# --- B3: each chart's exact archive is pinned by sha256 -------------------------------------


def test_recorded_scope_pins_every_chart_archive_by_sha256() -> None:
    import yaml

    scope = yaml.safe_load((REPO_ROOT / "config" / "bootstrap_scope.yaml").read_text())
    for release, chart in scope["charts"].items():
        digest = chart.get("chart_sha256", "")
        assert digest.startswith("sha256:") and len(digest) == 71, release


# --- harness credential is never briefly world-readable --------------------------------------


def test_harness_kubeconfig_is_private_from_creation(tmp_path) -> None:
    import base64

    from safeops.bootstrap import issue_harness_credential

    cfg = _cfg(tmp_path)
    cfg.kubeconfig_path.parent.mkdir(parents=True)
    cfg.kubeconfig_path.write_text(
        json.dumps(
            {"clusters": [{"name": cfg.kube_context, "cluster": {"server": "https://127.0.0.1:1"}}]}
        )
    )
    claims = (
        base64.urlsafe_b64encode(json.dumps({"iat": 0, "exp": cfg.harness_session_s}).encode())
        .decode()
        .rstrip("=")
    )

    class FakeKubectl:
        def create_token(self, *a):
            return f"h.{claims}.s"

    issue_harness_credential(cfg, FakeKubectl())
    assert stat.S_IMODE(cfg.harness_kubeconfig_path.stat().st_mode) == 0o600
    src = (REPO_ROOT / "safeops" / "bootstrap.py").read_text()
    assert "os.O_EXCL, 0o600" in src  # created 0600, not chmod'ed afterwards


# --- W1-W6: the harness doesn't trust the generator's summary -------------------------------

SETTINGS = WorkloadSettings("/checkout", 10, 1.0, 15, 300, 297, 60, 1, 120)
GOOD = {
    "state": "complete",
    "size": 300,
    "scheduled": 300,
    "succeeded": 299,
    "failed": 1,
    "deadline_s": 1.0,
    "success_rate": 299 / 300,
    "p95_latency_ms": 12.0,
}


class Proxy:
    def __init__(self, summary=None, stats_error=None, start_error=None):
        self.summary, self.stats_error, self.start_error = summary, stats_error, start_error
        self.actions: list[str] = []

    def control(self, body):
        self.actions.append(body["action"])
        if body["action"] == "start" and self.start_error:
            raise self.start_error
        return {"window_id": "w1"} if body["action"] == "measure" else {}

    def stats(self, window_id=None):
        if self.stats_error:
            raise self.stats_error
        return self.summary


def _measure(proxy):
    gen = WorkloadGenerator(proxy, SETTINGS, clock=lambda: 1e6, sleep=lambda s: None)
    gen._started_at = 0.0
    return gen.verify_steady_state()


def test_consistent_summary_is_still_a_valid_baseline() -> None:
    check_steady_state(_measure(Proxy(GOOD)))


@pytest.mark.parametrize(
    "patch",
    [
        {"succeeded": 301, "failed": -1},  # W1: more successes than attempts
        {"succeeded": 297, "failed": 0},  # W2: counts don't add up to 300
        {"deadline_s": 5.0},  # W3: measured with the wrong deadline
        {"succeeded": None},  # W4: missing counts
        {"succeeded": True},  # bool is not a count
        {"scheduled": 299},
        {"size": 100, "scheduled": 100, "succeeded": 100, "failed": 0},
    ],
)
def test_inconsistent_summary_is_a_measurement_failure_not_a_baseline(patch) -> None:
    result = _measure(Proxy({**GOOD, **patch}))
    with pytest.raises(SteadyStateMeasurementFailed, match="inconsistent"):
        check_steady_state(result)


def test_lost_window_is_a_measurement_failure() -> None:  # W6
    lost = CommandError("kubectl get failed: Error from server (NotFound): unknown window")
    with pytest.raises(SteadyStateMeasurementFailed, match="lost"):
        check_steady_state(_measure(Proxy(stats_error=lost)))


def test_busy_generator_fails_loudly_and_is_not_stopped() -> None:  # W5
    busy = CommandError("kubectl replace failed: Error from server (Conflict): already running")
    proxy = Proxy(start_error=busy)
    gen = WorkloadGenerator(proxy, SETTINGS)
    with pytest.raises(WorkloadGeneratorBusy, match="workload-stop"):
        gen.start_workload()
    assert issubclass(WorkloadGeneratorBusy, SteadyStateError)
    assert proxy.actions == ["start"]  # never auto-stopped: that could clobber a concurrent run


def test_workload_stop_command_exists() -> None:
    from typer.testing import CliRunner

    from safeops.cli import app

    assert "workload-stop" in CliRunner().invoke(app, ["--help"]).output


# --- L6: the agent can't see harness traffic in Prometheus ----------------------------------


def test_generator_scrape_keeps_only_loadgen_metrics() -> None:
    import yaml

    values = yaml.safe_load((REPO_ROOT / "manifests/helm/prometheus-values.yaml").read_text())
    job = values["scrapeConfigs"]["safeops-workload-generator"]
    assert job["metric_relabel_configs"] == [
        {"source_labels": ["__name__"], "action": "keep", "regex": "loadgen_.*"}
    ]


# --- testbed HTTP body parsing (chunked bodies from the API-server proxy) -------------------

_spec = importlib.util.spec_from_file_location(
    "testbed_httpbody", REPO_ROOT / "testbed/app/httpbody.py"
)
httpbody = importlib.util.module_from_spec(_spec)
sys.modules["testbed_httpbody"] = httpbody
_spec.loader.exec_module(httpbody)


def _headers(**kv) -> Message:
    m = Message()
    for k, v in kv.items():
        m[k.replace("_", "-")] = v
    return m


def test_chunked_body_with_extensions_and_trailers() -> None:
    raw = b'5;ext=1\r\n{"a":\r\n3\r\n 1}\r\n0\r\nX-Trailer: y\r\n\r\n'
    assert httpbody.read_body(_headers(Transfer_Encoding="chunked"), io.BytesIO(raw)) == b'{"a": 1}'


def test_content_length_body() -> None:
    assert httpbody.read_body(_headers(Content_Length="2"), io.BytesIO(b"{}")) == b"{}"
    assert httpbody.read_body(_headers(), io.BytesIO(b"")) == b""


@pytest.mark.parametrize(
    ("headers", "raw"),
    [
        ({"Transfer_Encoding": "chunked"}, b"zz\r\n"),  # bad size
        ({"Transfer_Encoding": "chunked"}, b"5\r\nab"),  # truncated chunk
        ({"Transfer_Encoding": "chunked"}, b"2\r\nab"),  # missing CRLF
        ({"Transfer_Encoding": "chunked"}, b""),  # closed early
        ({"Content_Length": "10"}, b"short"),
        ({"Content_Length": "-1"}, b""),
        ({"Content_Length": "nope"}, b""),
    ],
)
def test_malformed_bodies_are_rejected(headers, raw) -> None:
    with pytest.raises(httpbody.BodyError):
        httpbody.read_body(_headers(**headers), io.BytesIO(raw))


def test_oversized_bodies_are_rejected() -> None:
    big = b"%x\r\n" % 64 + b"a" * 64 + b"\r\n0\r\n\r\n"
    with pytest.raises(httpbody.BodyError):
        httpbody.read_body(_headers(Transfer_Encoding="chunked"), io.BytesIO(big), limit=32)
    with pytest.raises(httpbody.BodyError):
        httpbody.read_body(_headers(Content_Length="64"), io.BytesIO(b"a" * 64), limit=32)


# ============================== second review ==============================================

# --- C1: runtime refuses the admin credential however it's passed ---------------------------


def test_runtime_refuses_admin_kubeconfig_passed_explicitly(tmp_path) -> None:
    from safeops.cluster import CredentialError

    cfg = _cfg(tmp_path)
    with pytest.raises(CredentialError, match="never falls back"):
        Kubectl(cfg, Phase.RUNTIME, runner=Runner(), kubeconfig=cfg.kubeconfig_path)


def test_only_cluster_and_bootstrap_modules_touch_the_admin_kubeconfig_path() -> None:
    import ast

    users = set()
    for path in (REPO_ROOT / "safeops").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "kubeconfig_path":
                users.add(path.relative_to(REPO_ROOT).as_posix())
    assert users <= {"safeops/cluster.py", "safeops/bootstrap.py"}, users


# --- C2: the evaluator path only runs with the harness credential ---------------------------


def test_workload_proxy_refuses_a_bootstrap_kubectl(tmp_path) -> None:
    from safeops.cluster import WorkloadProxy

    cfg = _cfg(tmp_path)
    admin = Kubectl(
        cfg, Phase.BOOTSTRAP, runner=Runner(), kubeconfig=tmp_path / "kc", verify_identity=False
    )
    with pytest.raises(GuardError, match="runtime"):
        WorkloadProxy(cfg, admin)


# --- C3: the context must really be the kind cluster ---------------------------------------


def _kubeconfig(path: Path, server: str) -> Path:
    import yaml

    path.write_text(
        yaml.safe_dump(
            {
                "clusters": [{"name": "kind-safeops", "cluster": {"server": server}}],
                "contexts": [
                    {"name": "kind-safeops", "context": {"cluster": "kind-safeops", "user": "u"}}
                ],
                "users": [{"name": "u", "user": {}}],
            }
        )
    )
    return path


class DockerPortRunner(Runner):
    def __init__(self, published: str) -> None:
        super().__init__()
        self.published = published

    def __call__(self, args, input=None, **kwargs):
        self.calls.append(list(args))
        if args[:2] == ["docker", "port"]:
            return subprocess.CompletedProcess(args, 0, self.published, "")
        return subprocess.CompletedProcess(args, 0, "{}", "")


def test_identity_check_accepts_the_kind_node_port(tmp_path) -> None:
    kc = _kubeconfig(tmp_path / "kc", "https://127.0.0.1:55432")
    runner = DockerPortRunner("127.0.0.1:55432\n")
    Kubectl(_cfg(tmp_path), Phase.BOOTSTRAP, runner=runner, kubeconfig=kc).read("get", "pods")
    assert runner.calls[0] == ["docker", "port", "safeops-control-plane", "6443/tcp"]


@pytest.mark.parametrize(
    ("server", "published"),
    [
        ("https://127.0.0.1:8443", "127.0.0.1:55432\n"),  # a different local cluster
        ("https://192.168.49.2:8443", "127.0.0.1:55432\n"),  # minikube-style address
        ("https://127.0.0.1:55432", ""),  # no kind node at all
    ],
)
def test_identity_check_refuses_anything_else(tmp_path, server, published) -> None:
    kc = _kubeconfig(tmp_path / "kc", server)
    runner = DockerPortRunner(published)
    with pytest.raises(GuardError, match="not the API server"):
        Kubectl(_cfg(tmp_path), Phase.BOOTSTRAP, runner=runner, kubeconfig=kc).read("get", "pods")
    assert not [c for c in runner.calls if c[0] == "kubectl"]  # nothing ran


# --- M1: a closed window reads the same every time -----------------------------------------


def test_closed_window_is_frozen() -> None:
    spec = importlib.util.spec_from_file_location("tw2", REPO_ROOT / "testbed/app/window.py")
    w = importlib.util.module_from_spec(spec)
    sys.modules["tw2"] = w
    spec.loader.exec_module(w)
    win = w.Window("w1", 3, 1.0)
    late = None
    for i in range(3):
        a = w.Attempt(seq=i, due=i * 0.1, dispatched=i * 0.1)
        if i < 2:
            a.status, a.completed = 200, a.dispatched + 0.05
        else:
            late = a
        win.admit(a)
    close = 0.2 + 1.0 + 0.5
    first = win.summary(close)
    late.status, late.completed = 200, late.dispatched + 0.9  # recorded after close
    assert win.summary(close + 60) == first
    assert first["succeeded"] == 2


def test_generator_freezes_windows_at_close_and_guards_runs() -> None:
    src = (REPO_ROOT / "testbed/app/loadgen.py").read_text()
    assert "_freeze_closed_windows" in src and 'name="window-freezer"' in src
    assert '_state["generation"] != generation' in src  # M2


# --- M3: only Prometheus reaches the generator over the pod network ------------------------


def test_generator_network_policy_admits_only_monitoring() -> None:
    from safeops import manifests
    from safeops.cluster import load_cluster_config

    cfg = load_cluster_config()
    values = manifests.template_values(cfg, {"testbed": "t:x", "loadgen": "l:x"})
    objs = [
        o for f in manifests.manifest_files("workload") for o in manifests.load_objects(f, values)
    ]
    (policy,) = [o for o in objs if o["kind"] == "NetworkPolicy"]
    assert policy["metadata"]["namespace"] == cfg.workload_namespace
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "workload-generator"}}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    (rule,) = policy["spec"]["ingress"]
    assert rule["from"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": cfg.monitoring_namespace}
            }
        }
    ]


def test_exit_code_table_lists_every_code() -> None:
    text = (REPO_ROOT / "docs/decisions/0004-milestone-1-acceptance-command.md").read_text()
    for code in ("| 0 |", "| 2 |", "| 3 |", "| 4 |", "| 5 |"):
        assert code in text


# --- closeout: raw() paths must be absolute ------------------------------------------------


@pytest.mark.parametrize("phase", [Phase.RUNTIME, Phase.BOOTSTRAP])
@pytest.mark.parametrize(
    "path",
    ["--kubeconfig=/Users/x/.kube/config", "--context=minikube", "api/v1/namespaces", "", "-s"],
)
def test_raw_requires_an_absolute_path(tmp_path, phase, path) -> None:
    runner = Runner()
    k = Kubectl(
        _cfg(tmp_path), phase, runner=runner, kubeconfig=tmp_path / "kc", verify_identity=False
    )
    with pytest.raises(GuardError, match="must start with '/'"):
        k.raw("GET", path)
    assert runner.calls == []


def test_raw_still_accepts_absolute_bootstrap_paths(tmp_path) -> None:
    runner = Runner()
    k = Kubectl(
        _cfg(tmp_path),
        Phase.BOOTSTRAP,
        runner=runner,
        kubeconfig=tmp_path / "kc",
        verify_identity=False,
    )
    k.raw(
        "GET",
        "/api/v1/namespaces/safeops-monitoring/services/prometheus-server:80/proxy/api/v1/targets",
    )
    assert runner.calls[0][5:7] == ["get", "--raw"]
