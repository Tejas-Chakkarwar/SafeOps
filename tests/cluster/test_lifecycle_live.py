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
