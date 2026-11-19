"""Milestone 3 acceptance, live (decisions 0012, 0013). Run with `make cluster-test`.

The code under test (EnvironmentManager) runs exactly as the harness does: runtime phase,
experiment-controller credential. Test-only setup (corrupting state, planting residue,
creating a Chaos CR) uses the admin credential or host Docker, deliberately bypassing the
guard, because it plays the part of a fault or a misbehaving agent.

Order matters: the quarantine test rebuilds the cluster, so it runs last.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from safeops.cluster import load_cluster_config
from safeops.environment import snapshot as snap
from safeops.environment.manager import EnvironmentManager

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFEOPS_CLUSTER_TESTS") != "1",
    reason="needs a live cluster (make cluster-test)",
)

CFG = load_cluster_config()
NODE = f"{CFG.cluster_name}-control-plane"
NS = CFG.namespace


def admin(*args: str, stdin: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["kubectl", "--kubeconfig", str(CFG.kubeconfig_path), "--context", CFG.kube_context, *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout


def node_sh(script: str, detach: bool = False) -> str:
    args = ["docker", "exec"] + (["-d"] if detach else []) + [NODE, "sh", "-c", script]
    return subprocess.run(args, capture_output=True, text=True, timeout=60, check=True).stdout


def pod_pid(app: str) -> str:
    """Host PID of a testbed container, for entering its network namespace."""
    cid = node_sh(
        f"crictl ps --label io.kubernetes.pod.namespace={NS} --name {app} -q | head -1"
    ).strip()
    assert cid, f"no running container for {app}"
    return node_sh(
        f"crictl inspect --output go-template --template '{{{{.info.pid}}}}' {cid}"
    ).strip()


@pytest.fixture(scope="module")
def env() -> EnvironmentManager:
    e = EnvironmentManager()
    if not e.verify_clean_state().clean:
        e.reset_environment()
    assert e.verify_clean_state().clean, "precondition: start from a clean testbed"
    return e


# --- deliberate corruption makes verify fail; reset restores --------------------------------------


def test_corruption_makes_verify_fail_and_reset_restores(env) -> None:
    admin("scale", "deployment/orders", "-n", NS, "--replicas=5")
    admin(
        "patch",
        "configmap/db-pool-config",
        "-n",
        NS,
        "--type=merge",
        "-p",
        '{"data":{"max_pool_size":"90"}}',
    )
    admin(
        "patch",
        "deployment/payment",
        "-n",
        NS,
        "--type=json",
        "-p",
        '[{"op":"remove","path":"/spec/template/spec/containers/0/readinessProbe"}]',
    )
    admin(
        "patch",
        "service/orders-svc",
        "-n",
        NS,
        "--type=merge",
        "-p",
        '{"spec":{"selector":{"app":"inventory"}}}',
    )
    admin("create", "configmap", "rogue-config", "-n", NS, "--from-literal=x=1")
    admin("delete", "service/payment-svc", "-n", NS)

    result = env.verify_clean_state()
    assert not result.clean
    text = "\n".join(result.problems)
    for expected in [
        "Deployment/orders.replicas: expected 2, got 5",
        "ConfigMap/db-pool-config.numeric.max_pool_size: expected 10.0, got 90.0",
        "Deployment/payment.containers[0].readinessProbe",
        "Service/orders-svc.endpoint_owner_services",  # repointed at inventory pods
        "ConfigMap/rogue-config: unexpected",
        "Service/payment-svc: missing",
    ]:
        assert expected in text, f"{expected!r} not reported in:\n{text}"

    env.reset_environment()
    after = env.verify_clean_state()
    assert after.clean, after.problems


# --- rollout and restart produce zero spurious diffs ---


def test_restart_produces_zero_snapshot_diffs(env) -> None:
    before = env.capture_snapshot()
    admin("rollout", "restart", "deployment/orders", "-n", NS)
    admin("rollout", "status", "deployment/orders", "-n", NS, "--timeout=180s")
    assert env.wait_stabilization_window()
    assert snap.diff(before, env.capture_snapshot()) == []


def test_rollout_produces_zero_snapshot_diffs(env) -> None:
    before = env.capture_snapshot()
    admin(
        "patch",
        "deployment/gateway",
        "-n",
        NS,
        "--type=merge",
        "-p",
        '{"spec":{"template":{"metadata":{"annotations":{"safeops.io/rollout-probe":"1"}}}}}',
    )
    admin("rollout", "status", "deployment/gateway", "-n", NS, "--timeout=180s")
    rs = json.loads(admin("get", "replicasets", "-n", NS, "-l", "app=gateway", "-o", "json"))[
        "items"
    ]
    assert len(rs) >= 2, "the patch should have produced a new ReplicaSet"
    assert env.wait_stabilization_window()
    assert snap.diff(before, env.capture_snapshot()) == []
    env.reset_environment()
    assert env.verify_clean_state().clean


# --- 0003 F3: node residue with no Chaos CR present must fail verification ------------------------


def test_planted_netem_with_no_chaos_object_fails_verify(env) -> None:
    pid = pod_pid("payment")
    node_sh(f"nsenter -t {pid} -n tc qdisc add dev eth0 root netem delay 50ms")
    try:
        assert not json.loads(admin("get", "networkchaos", "-n", NS, "-o", "json"))["items"]
        result = env.verify_clean_state()
        assert not result.clean
        assert any("netem" in p for p in result.problems), result.problems
    finally:
        node_sh(f"nsenter -t {pid} -n tc qdisc del dev eth0 root")  # remove the planted residue
    assert env.verify_clean_state().clean


def test_real_network_chaos_is_cleared_by_reset(env) -> None:
    """A real NetworkChaos: verify sees both the CR and its netem; reset deletes the CR,
    Chaos Mesh recovers, and verify independently confirms no residue."""
    chaos = (
        "apiVersion: chaos-mesh.org/v1alpha1\nkind: NetworkChaos\n"
        f"metadata: {{name: m3-delay, namespace: {NS}}}\n"
        "spec:\n  action: delay\n  mode: all\n"
        f"  selector: {{namespaces: [{NS}], labelSelectors: {{app: payment}}}}\n"
        "  delay: {latency: '100ms'}\n"
    )
    admin("apply", "-f", "-", stdin=chaos)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = env.verify_clean_state()
        if any("netem" in p for p in result.problems):
            break
        time.sleep(3)
    assert any("NetworkChaos/m3-delay" in p for p in result.problems), result.problems
    assert any("netem" in p for p in result.problems), result.problems
    env.reset_environment()
    after = env.verify_clean_state()
    assert after.clean, after.problems


# --- Milestone 3 review H1: nothing in the testbed is invisible to verify ---


def test_stray_objects_are_seen_and_config_ones_removed_by_reset(env) -> None:
    admin(
        "apply",
        "-f",
        "-",
        stdin=(
            "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\n"
            f"metadata: {{name: m3r-deny-orders, namespace: {NS}}}\n"
            "spec: {podSelector: {matchLabels: {app: orders}}, policyTypes: [Ingress]}\n"
        ),
    )
    admin("create", "quota", "m3r-quota", "-n", NS, "--hard=pods=50")
    admin("create", "serviceaccount", "m3r-sa", "-n", NS)
    admin(
        "run",
        "m3r-bare",
        "-n",
        NS,
        "--image=busybox:1.36",
        "--restart=Never",
        "--labels=app=m3r",
        "--command",
        "--",
        "sleep",
        "600",
    )
    try:
        result = env.verify_clean_state()
        text = "\n".join(result.problems)
        for expected in [
            "NetworkPolicy/m3r-deny-orders",
            "ResourceQuota/m3r-quota",
            "ServiceAccount/m3r-sa",
            "Pod/m3r-bare",
        ]:
            assert f"{expected}: unexpected object in the testbed" in text, text
        env.reset_environment()
        after = env.verify_clean_state()
        assert [p for p in after.problems if "unexpected" in p] == [
            "Pod/m3r-bare: unexpected object in the testbed"
        ]  # pods are left to quarantine
    finally:
        admin("delete", "pod", "m3r-bare", "-n", NS, "--ignore-not-found", "--wait=true")
    assert env.verify_clean_state().clean


# --- Milestone 3 review H2: no privileged pods in the testbed ---

PRIVILEGED_POD = (
    "apiVersion: v1\nkind: Pod\n"
    f"metadata: {{name: m3r-escape, namespace: {NS}}}\n"
    "spec:\n  hostPID: true\n  hostNetwork: true\n"
    "  containers: [{name: c, image: busybox:1.36, securityContext: {privileged: true},\n"
    "                volumeMounts: [{name: root, mountPath: /host}]}]\n"
    "  volumes: [{name: root, hostPath: {path: /}}]\n"
)


def test_pod_security_rejects_privileged_pods_even_for_admin() -> None:
    proc = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(CFG.kubeconfig_path),
            "--context",
            CFG.kube_context,
            "create",
            "--dry-run=server",
            "-f",
            "-",
        ],
        input=PRIVILEGED_POD,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0 and "violates PodSecurity" in proc.stderr, proc.stderr


def test_harness_cannot_get_a_privileged_pod_through_a_deployment() -> None:
    """The harness may create Deployments, but their pods are admitted under baseline."""
    deploy = (
        "apiVersion: apps/v1\nkind: Deployment\n"
        f"metadata: {{name: m3r-escape, namespace: {NS}}}\n"
        "spec:\n  selector: {matchLabels: {app: m3r-escape}}\n  template:\n"
        "    metadata: {labels: {app: m3r-escape}}\n"
        "    spec:\n      hostPID: true\n"
        "      containers: [{name: c, image: busybox:1.36, securityContext: {privileged: true}}]\n"
    )
    proc = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(CFG.harness_kubeconfig_path),
            "--context",
            CFG.kube_context,
            "create",
            "--dry-run=server",
            "-f",
            "-",
        ],
        input=deploy,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "would violate PodSecurity" in proc.stderr, proc.stderr


# --- Milestone 3 review L5: reset replaces Postgres every time, and that's a no-op ---


def test_statefulset_replace_is_a_no_op_when_nothing_drifted(env) -> None:
    def sts():
        return json.loads(admin("get", "statefulset", "postgres", "-n", NS, "-o", "json"))

    def pod_uid():
        return json.loads(admin("get", "pod", "postgres-0", "-n", NS, "-o", "json"))["metadata"][
            "uid"
        ]

    gen, uid = sts()["metadata"]["generation"], pod_uid()
    env.reset_environment()
    assert sts()["metadata"]["generation"] == gen and pod_uid() == uid  # no rollout, no restart
    assert env.verify_clean_state().clean


# --- Milestone 3 second review R5: an injected EndpointSlice is a stray, found fast ---


def test_injected_endpoint_slice_is_flagged_quickly_and_removed_by_reset(env) -> None:
    admin(
        "apply",
        "-f",
        "-",
        stdin=(
            "apiVersion: discovery.k8s.io/v1\nkind: EndpointSlice\n"
            f"metadata: {{name: m3r2-inject, namespace: {NS},\n"
            "  labels: {kubernetes.io/service-name: orders-svc}}\n"
            "addressType: IPv4\n"
            "endpoints: [{addresses: ['10.0.0.99'], conditions: {ready: true}}]\n"
            "ports: [{name: http, port: 8080, protocol: TCP}]\n"
        ),
    )
    started = time.monotonic()
    result = env.verify_clean_state()
    took = time.monotonic() - started
    assert "EndpointSlice/m3r2-inject: unexpected object in the testbed" in result.problems
    assert took < 60, f"verify stalled {took:.0f}s on a hand-made slice"
    env.reset_environment()
    after = env.verify_clean_state()
    assert after.clean, after.problems


# --- reset -> verify passes 10 times in a row ---


def test_reset_verify_ten_consecutive_times(env) -> None:
    durations = []
    for i in range(10):
        started = time.monotonic()
        env.reset_environment()
        result = env.verify_clean_state()
        durations.append(round(time.monotonic() - started, 1))
        assert result.clean, f"iteration {i + 1}: {result.problems}"
    print(f"\n  reset->verify durations (s): {durations}")


# --- quarantine: residue reset cannot fix -> quarantine -> rebuilt -> clean (runs last) ---


def test_planted_stress_process_fails_verify_and_quarantine_rebuilds(env) -> None:
    # /tmp on the kind node is mounted noexec, so the fake stress-ng lives in /root. It's a
    # renamed `sleep`: the check matches the process name, which is what Chaos Mesh's
    # stress-ng shows. The node is destroyed by the rebuild below.
    node_sh("cp /bin/sleep /root/stress-ng && nohup /root/stress-ng 900 >/dev/null 2>&1 &")
    time.sleep(1)
    assert "stress-ng" in node_sh("ps -eo comm="), "the planted residue must actually be running"
    assert not json.loads(admin("get", "stresschaos", "-n", NS, "-o", "json"))["items"]
    result = env.verify_clean_state()
    assert not result.clean
    assert any("stress-ng" in p for p in result.problems), result.problems

    env.reset_environment()  # reset never touches the node: residue survives it
    assert any("stress-ng" in p for p in env.verify_clean_state().problems)

    env.quarantine_and_rebuild(reason="m3 acceptance: planted stress-ng residue")
    assert not env._marker.exists(), "quarantine marker should be cleared after a clean rebuild"
    final = env.verify_clean_state()
    assert final.clean, final.problems
