"""Environment lifecycle (§11.1, decisions 0012, 0013), tested offline with fakes.
The live acceptance (10× reset, corruption, F3 residue, quarantine, zero spurious diffs)
is in tests/cluster/test_lifecycle_live.py.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from safeops import manifests
from safeops.cluster import REPO_ROOT, CommandError, parse_cluster_config
from safeops.environment.desired import load_desired_state
from safeops.environment.manager import (
    CleanStateResult,
    EnvironmentManager,
    QuarantineFailed,
    WorkerQuarantined,
    stability_problems,
)
from safeops.environment.node import NodeCheckError, NodeInspector


def _cfg(root: Path):
    import yaml

    return parse_cluster_config(
        yaml.safe_load((REPO_ROOT / "config/cluster.yaml").read_text()), root=root
    )


def _desired_objects(cfg):
    values = manifests.template_values(cfg, manifests.image_refs())
    return [
        o for f in manifests.manifest_files("testbed") for o in manifests.load_objects(f, values)
    ]


class FakeKubectl:
    """Serves a fixed set of live objects; records every mutation, in order."""

    KIND_OF = {
        "deployment": "Deployment",
        "service": "Service",
        "configmap": "ConfigMap",
        "statefulset": "StatefulSet",
        "persistentvolumeclaim": "PersistentVolumeClaim",
    }

    def __init__(self, live: list[dict], chaos: list[dict] | None = None) -> None:
        self.live, self.chaos, self.ops = live, chaos or [], []

    def get_json(self, kinds, *_):
        items = []
        for resource in kinds.split(","):
            if resource == "networkchaos.chaos-mesh.org":
                items += self.chaos
            elif resource in self.KIND_OF:
                items += [o for o in self.live if o["kind"] == self.KIND_OF[resource]]
        return {"items": items}

    def read(self, *args):
        assert args[0] == "api-resources"
        return "networkchaos   chaos-mesh.org/v1alpha1   true   NetworkChaos\n"

    def delete(self, resource, kind, name, namespace):
        self.ops.append(("delete", kind, name))

    def create(self, objs):
        self.ops += [("create", o["kind"], o["metadata"]["name"]) for o in objs]

    def replace(self, objs):
        self.ops += [("replace", o["kind"], o["metadata"]["name"]) for o in objs]


def _env(tmp_path, kubectl=None, node=None, rebuild=None) -> EnvironmentManager:
    env = EnvironmentManager(kubectl=kubectl, node=node, rebuild=rebuild, sleep=lambda s: None)
    env._cfg = _cfg(tmp_path)
    return env


# --- reset ----------------------------------------------------------------------------------


def test_reset_order_chaos_first_then_extraneous_then_app_tier_recreated(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    desired = _desired_objects(cfg)
    live = copy.deepcopy(desired)
    next(o for o in live if o["metadata"]["name"] == "orders")["spec"]["replicas"] = 9  # drift
    live += [
        {"kind": "ConfigMap", "metadata": {"name": "rogue"}},
        {"kind": "ConfigMap", "metadata": {"name": "kube-root-ca.crt"}},
        {"kind": "PersistentVolumeClaim", "metadata": {"name": "data-postgres-0"}},
        {"kind": "PersistentVolumeClaim", "metadata": {"name": "stray-claim"}},
    ]
    k = FakeKubectl(live, chaos=[{"metadata": {"name": "delay-orders"}}])
    env = _env(tmp_path, kubectl=k)
    env.wait_stabilization_window = lambda: True
    env.reset_environment()

    ops = k.ops
    assert ops[0] == ("delete", "NetworkChaos", "delay-orders")  # chaos first
    assert ("delete", "ConfigMap", "rogue") in ops
    assert ("delete", "PersistentVolumeClaim", "stray-claim") in ops
    assert ("delete", "ConfigMap", "kube-root-ca.crt") not in ops  # auto-created, kept
    assert ("delete", "PersistentVolumeClaim", "data-postgres-0") not in ops  # data never touched
    assert (
        "replace",
        "StatefulSet",
        "postgres",
    ) in ops  # always PUT; no-op if unchanged (M3 review L5)
    app = [
        (o["kind"], o["metadata"]["name"])
        for o in desired
        if o["kind"] in ("Deployment", "Service", "ConfigMap")
    ]
    for kind, name in app:  # every app object recreated
        assert ops.index(("delete", kind, name)) < ops.index(("create", kind, name))
    deletes_dep = max(ops.index(("delete", "Deployment", n)) for k_, n in app if k_ == "Deployment")
    first_create = min(ops.index(("create", k_, n)) for k_, n in app)
    assert deletes_dep < first_create  # old pods gone before new


def test_reset_replaces_a_drifted_statefulset_but_never_its_pvc(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    live = copy.deepcopy(_desired_objects(cfg))
    sts = next(o for o in live if o["kind"] == "StatefulSet")
    sts["spec"]["template"]["spec"]["containers"][0]["image"] = "postgres:drifted"
    k = FakeKubectl(live)
    env = _env(tmp_path, kubectl=k)
    env.wait_stabilization_window = lambda: True
    env.reset_environment()
    assert ("replace", "StatefulSet", "postgres") in k.ops


def test_reset_refuses_on_a_quarantined_worker(tmp_path) -> None:
    env = _env(tmp_path, kubectl=FakeKubectl([]))
    env._marker.parent.mkdir(parents=True)
    env._marker.write_text('{"reason": "x"}')
    with pytest.raises(WorkerQuarantined):
        env.reset_environment()


# --- verify (independent of reset; fail closed) ------------------------------------------------


class FakeNode:
    def __init__(self, residue=None, error=None):
        self._residue, self._error = residue or [], error

    def residue(self):
        if self._error:
            raise self._error
        return list(self._residue)


def _verify_env(tmp_path, snapshot=None, chaos=(), node=None, stable=True):
    env = _env(tmp_path, kubectl=FakeKubectl([]), node=node or FakeNode())
    env.wait_stabilization_window = lambda: stable
    env.capture_snapshot = lambda: snapshot if snapshot is not None else load_desired_state()
    env._chaos_objects = lambda: list(chaos)
    return env


def test_verify_clean_when_everything_matches(tmp_path) -> None:
    assert _verify_env(tmp_path).verify_clean_state() == CleanStateResult(True, ())


def test_verify_flags_drift(tmp_path) -> None:
    drifted = load_desired_state()
    drifted.resources[("Deployment", "orders")]["replicas"] = 5
    result = _verify_env(tmp_path, snapshot=drifted).verify_clean_state()
    assert not result.clean
    assert "Deployment/orders.replicas: expected 2, got 5" in result.problems


def test_verify_flags_leftover_chaos_object(tmp_path) -> None:
    result = _verify_env(
        tmp_path, chaos=[("networkchaos.chaos-mesh.org", "NetworkChaos", "x")]
    ).verify_clean_state()
    assert not result.clean and any("NetworkChaos/x" in p for p in result.problems)


@pytest.mark.parametrize(
    "residue",
    [
        ["netem qdisc in network namespace net:[4026532]: qdisc netem 1: root"],
        ["stress-ng process on the node (pid 77): stress-ng --cpu 2"],
    ],
)
def test_verify_flags_node_residue_with_no_chaos_object(tmp_path, residue) -> None:
    """0003 F3: residue with no Chaos CR present must still be dirty."""
    result = _verify_env(tmp_path, chaos=(), node=FakeNode(residue=residue)).verify_clean_state()
    assert not result.clean and result.problems == tuple(residue)


def test_verify_fails_closed_when_the_node_check_cannot_run(tmp_path) -> None:
    node = FakeNode(error=NodeCheckError("container is not the SafeOps kind control-plane"))
    result = _verify_env(tmp_path, node=node).verify_clean_state()
    assert not result.clean and "fail closed" in result.problems[0]


def test_verify_fails_closed_when_cluster_reads_fail(tmp_path) -> None:
    env = _verify_env(tmp_path)

    def broken():
        raise CommandError("kubectl get failed: connection refused")

    env.capture_snapshot = broken
    result = env.verify_clean_state()
    assert not result.clean and "fail closed" in result.problems[0]


def test_verify_flags_unstable_testbed(tmp_path) -> None:
    result = _verify_env(tmp_path, stable=False).verify_clean_state()
    assert not result.clean and "not stable" in result.problems[0]


# --- stabilization (pure) -----------------------------------------------------------------------


def _dep(name, replicas=2, **status):
    st = {
        "observedGeneration": 2,
        "replicas": replicas,
        "updatedReplicas": replicas,
        "readyReplicas": replicas,
        "availableReplicas": replicas,
        **status,
    }
    return {
        "metadata": {"name": name, "generation": 2},
        "spec": {"replicas": replicas},
        "status": st,
    }


def test_stable_testbed_has_no_problems() -> None:
    assert stability_problems([_dep("orders")], [], [], [], []) == []


@pytest.mark.parametrize(
    "status",
    [
        {"observedGeneration": 1},
        {"readyReplicas": 1},
        {"updatedReplicas": 1},
        {"availableReplicas": 1},
    ],
)
def test_rollouts_in_flight_are_unstable(status) -> None:
    assert stability_problems([_dep("orders", **status)], [], [], [], [])


def test_surplus_old_replicas_are_unstable() -> None:
    dep = _dep("orders")
    dep["status"]["replicas"] = 3  # an old pod still counted during a rollout
    assert stability_problems([dep], [], [], [], [])


def test_terminating_pods_and_lagging_endpoints_are_unstable() -> None:
    pod = {
        "metadata": {"name": "orders-1", "labels": {"app": "orders"}},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    svc = {"metadata": {"name": "orders-svc"}, "spec": {"selector": {"app": "orders"}}}
    lagging = [
        {"metadata": {"labels": {"kubernetes.io/service-name": "orders-svc"}}, "endpoints": []}
    ]
    assert stability_problems([], [], [svc], lagging, [pod])
    dying = copy.deepcopy(pod)
    dying["metadata"]["deletionTimestamp"] = "now"
    assert stability_problems([], [], [], [], [dying])


# --- quarantine -----------------------------------------------------------------------------------


def test_quarantine_marks_first_rebuilds_then_clears_on_clean(tmp_path) -> None:
    seen = {}
    env = _verify_env(tmp_path)

    def rebuild():
        seen["marked_during_rebuild"] = env._marker.is_file()

    env._rebuild = rebuild
    env.quarantine_and_rebuild(reason="post-run: drift")
    assert seen["marked_during_rebuild"] is True
    assert not env._marker.exists()


def test_quarantine_that_stays_dirty_halts_the_worker(tmp_path) -> None:
    drifted = load_desired_state()
    drifted.resources[("Deployment", "orders")]["replicas"] = 0
    env = _verify_env(tmp_path, snapshot=drifted)
    env._rebuild = lambda: None
    with pytest.raises(QuarantineFailed):
        env.quarantine_and_rebuild(reason="x")
    assert json.loads(env._marker.read_text())["reason"] == "x"  # marker stays: halted
    with pytest.raises(WorkerQuarantined):
        env.reset_environment()


def test_failed_rebuild_keeps_the_worker_quarantined(tmp_path) -> None:
    env = _verify_env(tmp_path)

    def rebuild():
        raise QuarantineFailed("cluster-up failed during quarantine rebuild")

    env._rebuild = rebuild
    with pytest.raises(QuarantineFailed):
        env.quarantine_and_rebuild()
    assert env._marker.is_file()


# --- runner hooks ---


def test_pre_run_dirty_quarantines_and_discards(offline_runner_factory) -> None:
    from safeops.experiment.runner import PreRunDiscard

    r = offline_runner_factory()
    with pytest.raises(PreRunDiscard, match="discarded"):
        r._require_pre_run_clean(CleanStateResult(False, ("Deployment/orders: drift",)))
    assert r.environment.calls == ["quarantine_and_rebuild"]
    r._require_pre_run_clean(CleanStateResult(True, ()))  # clean: nothing happens


def test_post_run_dirty_keeps_the_run_and_quarantines(offline_runner_factory) -> None:
    r = offline_runner_factory()
    assert r._post_run_clean(CleanStateResult(True, ())) is True
    assert r._post_run_clean(CleanStateResult(False, ("x",))) is False
    assert r.environment.calls == ["quarantine_and_rebuild"]
    assert r._halt is None


def test_post_run_failed_rebuild_halts_after_the_record(offline_runner_factory) -> None:
    r = offline_runner_factory()

    def fail(reason=""):
        raise QuarantineFailed("still dirty")

    r.environment.quarantine_and_rebuild = fail
    assert r._post_run_clean(CleanStateResult(False, ("x",))) is False
    assert isinstance(r._halt, QuarantineFailed)  # raised by run() only after finalize


# --- node inspector (host-side docker exec, fail closed) ---


class Docker:
    def __init__(
        self, labels="safeops|control-plane", port="127.0.0.1:55432\n", qdisc="", ps="", fail=None
    ):
        self.labels, self.port, self.qdisc, self.ps, self.fail, self.calls = (
            labels,
            port,
            qdisc,
            ps,
            fail,
            [],
        )

    def __call__(self, args, **kw):
        self.calls.append(args)
        if self.fail and args[1] == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "Error: No such container")
        out = {"inspect": self.labels, "port": self.port}.get(args[1])
        if out is None:
            out = self.qdisc if "tc qdisc" in args[-1] else self.ps
        return subprocess.CompletedProcess(args, 0, out, "")


SERVER = "https://127.0.0.1:55432"


def test_node_clean() -> None:
    d = Docker(
        qdisc="net:[1] qdisc noqueue 0: dev lo root refcnt 2\n",
        ps="  1 systemd /sbin/init\n  42 containerd /usr/local/bin/containerd\n",
    )
    assert NodeInspector(_cfg(REPO_ROOT), runner=d, expected_api_server=SERVER).residue() == []
    assert d.calls[0][:3] == ["docker", "inspect", "safeops-control-plane"]


def test_node_finds_netem_and_tbf_in_any_namespace() -> None:
    d = Docker(
        qdisc="net:[1] qdisc noqueue 0: dev lo root\n"
        "net:[4026532901] qdisc netem 1: dev eth0 root refcnt 2 limit 1000 delay 200ms\n"
        "net:[4026532999] qdisc tbf 1: dev eth0 root rate 1Mbit\n"
    )
    found = NodeInspector(_cfg(REPO_ROOT), runner=d, expected_api_server=SERVER).qdisc_residue()
    assert len(found) == 2 and "netem" in found[0] and "tbf" in found[1]


@pytest.mark.parametrize(
    "ps",
    [
        "  77 stress-ng stress-ng --cpu 2 --cpu-load 100\n",
        "  78 memStress memStress --size 100M\n",
        "  79 sleep /tmp/stress-ng 300\n",
    ],
)  # argv0 renamed, comm differs
def test_node_finds_stress_processes(ps) -> None:
    found = NodeInspector(
        _cfg(REPO_ROOT), runner=Docker(ps=ps), expected_api_server=SERVER
    ).process_residue()
    assert len(found) == 1


@pytest.mark.parametrize(
    "docker",
    [
        Docker(labels="other-cluster|control-plane"),
        Docker(labels="safeops|worker"),
        Docker(port="127.0.0.1:9999\n"),
        Docker(port=""),
        Docker(fail="inspect"),
        Docker(fail="exec"),
    ],
    ids=["wrong-cluster", "wrong-role", "wrong-port", "no-port", "no-container", "exec-fails"],
)
def test_node_identity_or_inspection_failure_fails_closed(docker) -> None:
    with pytest.raises(NodeCheckError):
        NodeInspector(_cfg(REPO_ROOT), runner=docker, expected_api_server=SERVER).residue()
    assert not [c for c in docker.calls if c[1] == "exec"] or docker.fail == "exec"


def test_node_inspection_scripts_take_no_caller_input() -> None:
    from safeops.environment import node

    assert "{" not in node._PROCESS_SCRIPT and "format(" not in node._QDISC_SCRIPT
