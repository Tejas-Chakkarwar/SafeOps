"""Live-cluster tests. Run with `make cluster-test` after `make cluster-up`.

Skipped unless SAFEOPS_CLUSTER_TESTS=1, so `make test` never touches a cluster.
Each one settles a question by asking the real API server, not by doctrine (decision 0009).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from safeops.cluster import Kubectl, Phase, WorkloadProxy, load_cluster_config

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFEOPS_CLUSTER_TESTS") != "1",
    reason="needs a live cluster (make cluster-test)",
)

CFG = load_cluster_config()
SA = f"system:serviceaccount:{CFG.system_namespace}:experiment-controller"


def _harness_raw_get(path: str) -> subprocess.CompletedProcess:
    """A raw GET with the harness credential that deliberately bypasses WorkloadProxy's
    capability check, so it shows what the API server itself allows."""
    return subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(CFG.harness_kubeconfig_path),
            "--context",
            CFG.kube_context,
            "get",
            "--raw",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _admin_raw_get(path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(CFG.kubeconfig_path),
            "--context",
            CFG.kube_context,
            "get",
            "--raw",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="module")
def decoy_service():
    """A second Service in the workload namespace, created and removed with the admin
    credential, so denial is proven against a Service that really exists."""
    manifest = (
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: rbac-decoy\n"
        f"  namespace: {CFG.workload_namespace}\n"
        "spec:\n  selector: {app: workload-generator}\n  ports: [{port: 8080, targetPort: 8080}]\n"
    )
    base = ["kubectl", "--kubeconfig", str(CFG.kubeconfig_path), "--context", CFG.kube_context]
    subprocess.run(
        base + ["apply", "-f", "-"], input=manifest, text=True, check=True, capture_output=True
    )
    yield "rbac-decoy"
    subprocess.run(
        base
        + ["delete", "service", "rbac-decoy", "-n", CFG.workload_namespace, "--ignore-not-found"],
        check=True,
        capture_output=True,
    )


def test_harness_can_proxy_stats_and_control_to_the_workload_generator() -> None:
    proxy = WorkloadProxy(CFG)
    assert proxy.stats()["running"] in (True, False)
    # PUT /control reaches the generator (a 400 for an unknown action proves the PUT arrived).
    with pytest.raises(Exception, match="400|BadRequest|action must be"):
        proxy.control({"action": "nonsense"})


def test_harness_is_denied_for_another_service_in_the_same_namespace(decoy_service: str) -> None:
    path = f"/api/v1/namespaces/{CFG.workload_namespace}/services/{decoy_service}/proxy/stats"
    assert _admin_raw_get(path).returncode == 0, "decoy should be reachable as admin"
    denied = _harness_raw_get(path)
    assert denied.returncode != 0
    assert "forbidden" in denied.stderr.lower()


def test_harness_is_denied_everywhere_else() -> None:
    for path in [
        f"/api/v1/namespaces/{CFG.monitoring_namespace}/services/{CFG.prometheus_service}:"
        f"{CFG.prometheus_service_port}/proxy/api/v1/targets",
        f"/api/v1/namespaces/{CFG.namespace}/services/gateway-svc/proxy/checkout",
        f"/api/v1/namespaces/{CFG.namespace}/secrets",
        f"/api/v1/namespaces/{CFG.namespace}/pods",
    ]:
        proc = _harness_raw_get(path)
        assert proc.returncode != 0 and "forbidden" in proc.stderr.lower(), path


def test_rbac_verbs_are_get_and_update_only() -> None:
    def can_i(verb: str, resource: str) -> bool:
        proc = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(CFG.kubeconfig_path),
                "--context",
                CFG.kube_context,
                "auth",
                "can-i",
                verb,
                resource,
                "--subresource=proxy",  # can-i does not parse "services/<name>/proxy"
                "-n",
                CFG.workload_namespace,
                "--as",
                SA,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.stdout.strip() == "yes"

    proxy = f"services/{CFG.workload_service}"
    assert can_i("get", proxy) and can_i("update", proxy)
    assert not can_i("get", "services/some-other-service")
    for verb in ("create", "delete", "patch"):
        assert not can_i(verb, proxy), verb


def test_ownership_map_resolves_the_live_testbed() -> None:
    from safeops.bootstrap import ownership_check

    ownership_check(CFG, Kubectl(CFG, Phase.BOOTSTRAP))
