"""Milestone 6, live (decision 0023): facts a fake can't establish. Run with
`make cluster-test` after `make cluster-up`.

- safeops-reader, with its own token, can do every §4 read and nothing else; the API server
  refuses its writes (server-side dry runs: nothing is changed even if one were allowed).
- The eight tools parse real API-server, kubelet and Prometheus responses into results that
  pass their own contracts, strict JSON and bounds.
- The clock probe works through the real runtime path and leaves nothing behind.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest

from safeops.bootstrap import reader_rbac_expectations
from safeops.cluster import Kubectl, Phase, WorkloadProxy, load_cluster_config
from safeops.environment.clock import PROBE_PREFIX
from safeops.environment.manager import EnvironmentManager
from safeops.tools import contracts as c
from safeops.tools import (
    get_configmap,
    get_deployment,
    get_events,
    get_logs,
    get_metrics,
    get_pods,
    get_rollout_history,
    get_service,
)
from safeops.tools.canonical import canonical_json, result_digest
from safeops.tools.contracts import invoke, step_problems
from safeops.tools.reader import ToolContext

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFEOPS_CLUSTER_TESTS") != "1",
    reason="needs a live cluster (make cluster-test)",
)

CFG = load_cluster_config()
READER = [
    "kubectl",
    "--kubeconfig",
    str(CFG.reader_kubeconfig_path),
    "--context",
    CFG.kube_context,
]


def _reader_kubectl(*args: str) -> subprocess.CompletedProcess:
    """kubectl with the reader's own token, past every client-side guard: what the API
    server itself allows."""
    return subprocess.run(
        READER + list(args), capture_output=True, text=True, timeout=60, check=False
    )


def _can_i(verb, resource, sub, ns) -> bool:
    args = ["auth", "can-i", verb, resource, *([f"--subresource={sub}"] if sub else []), "-n", ns]
    return _reader_kubectl(*args).stdout.strip() == "yes"


# --- safeops-reader RBAC, with its own credential ------------------------------------------------


def test_the_reader_token_has_exactly_the_section_4_surface() -> None:
    wrong = [
        (verb, resource, sub, ns, want)
        for verb, resource, sub, ns, want in reader_rbac_expectations(CFG)
        if _can_i(verb, resource, sub, ns) != want
    ]
    assert not wrong, wrong


def test_pods_log_is_get_only() -> None:
    ns = CFG.namespace
    assert _can_i("get", "pods", "log", ns)
    for verb in ("list", "watch", "create", "update", "patch", "delete"):
        assert not _can_i(verb, "pods", "log", ns), verb


@pytest.mark.parametrize(
    "args",
    [
        ["create", "configmap", "reader-probe", "-n", CFG.namespace, "--dry-run=server"],
        ["scale", "deployment", "orders", "--replicas=3", "-n", CFG.namespace, "--dry-run=server"],
        ["delete", "pods", "--all", "-n", CFG.namespace, "--dry-run=server"],
        ["label", "deployment", "orders", "x=y", "-n", CFG.namespace, "--dry-run=server"],
        ["annotate", "service", "orders-svc", "x=y", "-n", CFG.namespace, "--dry-run=server"],
        [
            "patch",
            "deployment",
            "orders",
            "-n",
            CFG.namespace,
            "--dry-run=server",
            "-p",
            '{"spec":{"replicas":3}}',
        ],
        ["get", "secrets", "-n", CFG.namespace],
        ["get", "pods", "-n", CFG.workload_namespace],
        ["get", "pods", "-n", CFG.chaos_namespace],
        ["get", "networkchaos.chaos-mesh.org", "-n", CFG.namespace],
        ["get", "nodes"],
    ],
    ids=lambda a: " ".join(a[:2]),
)
def test_the_api_server_refuses_the_reader_s_writes_and_other_reads(args) -> None:
    proc = _reader_kubectl(*args)
    assert proc.returncode != 0 and "forbidden" in proc.stderr.lower(), (proc.stdout, proc.stderr)


def test_the_reader_cannot_reach_the_workload_proxy() -> None:
    path = (
        f"/api/v1/namespaces/{CFG.workload_namespace}/services/{CFG.workload_service}/proxy/stats"
    )
    proc = _reader_kubectl("get", "--raw", path)
    assert proc.returncode != 0 and "forbidden" in proc.stderr.lower()


# --- the eight tools on real responses ---------------------------------------------------------


@pytest.fixture(scope="module")
def ctx() -> ToolContext:
    return ToolContext.for_reader(not_before=datetime.now(UTC) - timedelta(minutes=30))


CALLS = [
    (get_pods, {"namespace": CFG.namespace}),
    (get_events, {"namespace": CFG.namespace, "last_minutes": c.MAX_MINUTES}),
    (get_logs, {"service": "gateway", "last_minutes": 30, "severity": "debug", "max_lines": 200}),
    (get_logs, {"service": "postgres", "last_minutes": 30, "severity": "info", "max_lines": 50}),
    (get_metrics, {"service": "gateway", "metric": "request_rate", "window": 10}),
    (get_metrics, {"service": "orders", "metric": "error_rate", "window": 10}),
    (get_metrics, {"service": "orders", "metric": "latency_p95_seconds", "window": 10}),
    (get_metrics, {"service": "orders", "metric": "cpu_usage_cores", "window": 60}),
    (get_metrics, {"service": "postgres", "metric": "memory_working_set_bytes", "window": 5}),
    (get_deployment, {"namespace": CFG.namespace, "name": "orders"}),
    (get_deployment, {"namespace": CFG.namespace, "name": "does-not-exist"}),
    (get_rollout_history, {"namespace": CFG.namespace, "name": "orders"}),
    (get_configmap, {"namespace": CFG.namespace, "name": "orders-config"}),
    (get_configmap, {"namespace": CFG.namespace, "name": "db-pool-config"}),
    (get_service, {"namespace": CFG.namespace, "name": "orders-svc"}),
    (get_service, {"namespace": CFG.namespace, "name": "postgres-svc"}),
]


@pytest.mark.parametrize(("tool", "args"), CALLS, ids=lambda x: getattr(x, "__name__", ""))
def test_each_tool_reads_the_real_cluster_within_its_contract(ctx, tool, args) -> None:
    result = invoke(tool, ctx, args)
    assert result["status"] == "ok", result
    assert step_problems(tool.__name__, args, result, result_digest(result)) == []
    canonical_json(result)  # strict JSON: nothing non-finite escaped
    data = result["data"]
    if tool is get_events:
        assert data["event_count"] <= c.MAX_EVENTS
    if tool is get_logs:
        assert data["line_count"] <= args["max_lines"] <= c.MAX_LOG_LINES
    if tool is get_metrics:
        assert data["point_count"] <= c.MAX_METRIC_POINTS


def test_real_shapes_are_what_the_testbed_should_show(ctx) -> None:
    pods = get_pods(ctx, CFG.namespace)
    assert pods["pod_count"] == pods["total_pods"] >= 9 and not pods["truncated"]
    assert all(p["phase"] == "Running" and p["ready"] for p in pods["pods"])
    deploy = get_deployment(ctx, CFG.namespace, "orders")
    assert deploy["found"] and deploy["current_revision"] >= 1 and deploy["replicas"] == 2
    env = {e["name"]: e for e in deploy["containers"][0]["env"]}
    assert (
        env["DB_PASSWORD"]["value_from"]["source"] == "secretKeyRef"
        and env["DB_PASSWORD"]["value"] is None
    )
    assert env["LOG_LEVEL"]["value_from"] == {
        "source": "configMapKeyRef",
        "name": "orders-config",
        "key": "log_level",
    }
    history = get_rollout_history(ctx, CFG.namespace, "orders")
    assert history["revision_count"] >= 1
    assert history["revisions"][-1]["revision"] == deploy["current_revision"]
    svc = get_service(ctx, CFG.namespace, "orders-svc")
    assert svc["selector"] == {"app": "orders"} and svc["ready_endpoint_count"] == 2
    config = get_configmap(ctx, CFG.namespace, "db-pool-config")
    assert {e["key"] for e in config["entries"]} >= {"max_pool_size", "connection_timeout_ms"}
    logs = get_logs(ctx, "orders", 30, "debug", 200)
    assert logs["unavailable"] == [] and {x["pod"].split("-")[0] for x in logs["lines"]} <= {
        "orders"
    }


def test_metrics_under_real_load_are_current_incident_rates() -> None:
    """A short real load: the request rate appears, from points no earlier than the load
    start plus one rate range, and the error ratio is a ratio (or the non-finite tag)."""
    proxy = WorkloadProxy(CFG)
    proxy.control({"action": "stop"})
    started = datetime.now(UTC)
    proxy.control({"action": "start", "rate_rps": 10, "deadline_s": 1.0, "path": "/checkout"})
    try:
        time.sleep(60)
        live = ToolContext.for_reader(not_before=started)
        rate = get_metrics(live, "gateway", "request_rate", 5)
        errors = get_metrics(live, "gateway", "error_rate", 5)
    finally:
        proxy.control({"action": "stop"})
    assert rate["point_count"] >= 3
    first = datetime.fromisoformat(rate["start"])
    assert first >= started + timedelta(seconds=rate["range_s"])
    finite = [p["v"] for p in rate["points"] if isinstance(p["v"], float)]
    assert finite and finite[-1] > 1.0  # about 10 requests/s at the end
    # Healthy traffic reads 0, never "no data" (Milestone 6 review): the 5xx series doesn't
    # exist until a first 5xx, so the ratio needs points of its own.
    assert errors["point_count"] >= 3
    for p in errors["points"]:
        assert p["v"] == {"non_finite": "NaN"} or 0.0 <= p["v"] <= 1.0
    assert errors["points"][-1]["v"] == 0.0  # steady traffic, no faults: exactly zero


# --- the clock probe, live ---------------------------------------------------------------------


def _probes_left() -> list[str]:
    harness = Kubectl(CFG, Phase.RUNTIME)
    items = harness.get_json("configmaps", "-n", CFG.namespace)["items"]
    return [i["metadata"]["name"] for i in items if i["metadata"]["name"].startswith(PROBE_PREFIX)]


def test_a_live_clock_reading_is_small_and_leaves_no_probe() -> None:
    env = EnvironmentManager()
    readings = [env.read_clock() for _ in range(2)]
    for r in readings:
        offset = r.offset_us / 1e6
        assert -5 < offset < 5, offset  # the kind control plane shares the Docker VM's clock
        assert r.server_created.microsecond == 0
        assert r.host_before <= r.host_after
    assert _probes_left() == []
    assert env.verify_clean_state().clean
