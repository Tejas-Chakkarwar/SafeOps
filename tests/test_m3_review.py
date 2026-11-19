"""Regression tests for the Milestone 3 adversarial review. Each test is labelled with the
finding it closes, and each reproduced the problem before the fix. Offline: no cluster.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from safeops import manifests
from safeops.cluster import REPO_ROOT, CommandError, GuardError, load_cluster_config
from safeops.environment import snapshot as snap
from safeops.environment.desired import load_desired_state
from safeops.environment.manager import (
    INVENTORY,
    CleanStateResult,
    EnvironmentManager,
    QuarantineFailed,
    unexpected_objects,
)

CFG = load_cluster_config()


def _meta(name, owner=None, **extra):
    m = {"name": name, **extra}
    if owner:
        m["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    return {"metadata": m}


DEPLOYMENTS, STATEFULSETS = {"orders", "gateway"}, {"postgres"}
RBAC = {
    ("Role", "experiment-controller-testbed-lifecycle"),
    ("RoleBinding", "experiment-controller-testbed-lifecycle"),
}


def _clean_inventory() -> dict:
    return {
        "replicasets.apps": [_meta("orders-abc", ("Deployment", "orders"))],
        "pods": [
            _meta("orders-abc-1", ("ReplicaSet", "orders-abc")),
            _meta("postgres-0", ("StatefulSet", "postgres")),
        ],
        "controllerrevisions.apps": [_meta("postgres-7d9", ("StatefulSet", "postgres"))],
        "serviceaccounts": [_meta("default")],
        "roles.rbac.authorization.k8s.io": [_meta("experiment-controller-testbed-lifecycle")],
        "rolebindings.rbac.authorization.k8s.io": [
            _meta("experiment-controller-testbed-lifecycle")
        ],
    }


# --- H1: nothing in the testbed is invisible ---------------------------------------------------


def test_a_clean_inventory_has_nothing_unexpected() -> None:
    assert unexpected_objects(_clean_inventory(), DEPLOYMENTS, STATEFULSETS, RBAC) == []


@pytest.mark.parametrize(
    ("resource", "obj", "kind", "deletable"),
    [
        ("networkpolicies.networking.k8s.io", _meta("deny-orders"), "NetworkPolicy", True),
        ("resourcequotas", _meta("quota"), "ResourceQuota", True),
        ("limitranges", _meta("limits"), "LimitRange", True),
        ("horizontalpodautoscalers.autoscaling", _meta("hpa"), "HorizontalPodAutoscaler", True),
        ("poddisruptionbudgets.policy", _meta("pdb"), "PodDisruptionBudget", True),
        ("jobs.batch", _meta("job"), "Job", True),
        ("cronjobs.batch", _meta("cron"), "CronJob", True),
        ("ingresses.networking.k8s.io", _meta("ing"), "Ingress", True),
        ("serviceaccounts", _meta("extra-sa"), "ServiceAccount", True),
        ("pods", _meta("bare-pod"), "Pod", False),  # no controller
        ("pods", _meta("foreign", ("ReplicaSet", "inventory-zzz")), "Pod", False),  # unknown RS
        ("replicasets.apps", _meta("orphan-rs"), "ReplicaSet", False),
        ("replicasets.apps", _meta("rogue-rs", ("Deployment", "rogue")), "ReplicaSet", False),
        (
            "controllerrevisions.apps",
            _meta("cr", ("StatefulSet", "other")),
            "ControllerRevision",
            False,
        ),
        ("roles.rbac.authorization.k8s.io", _meta("escalate"), "Role", False),
        ("rolebindings.rbac.authorization.k8s.io", _meta("escalate"), "RoleBinding", False),
    ],
)
def test_every_stray_object_is_flagged(resource, obj, kind, deletable) -> None:
    items = _clean_inventory()
    items.setdefault(resource, []).append(obj)
    assert unexpected_objects(items, DEPLOYMENTS, STATEFULSETS, RBAC) == [
        (resource, kind, obj["metadata"]["name"], deletable)
    ]


def test_inventory_never_includes_secrets() -> None:
    assert not [r for r in INVENTORY if "secret" in r]  # no SafeOps identity reads Secrets


class InventoryKubectl:
    """Live projected objects = desired; plus whatever inventory items a test plants."""

    def __init__(self, desired, inventory):
        self.desired, self.inventory, self.ops = desired, inventory, []

    def get_json(self, kinds, *_):
        if kinds in INVENTORY:
            return {"items": self.inventory.get(kinds, [])}
        names = {
            "deployment": "Deployment",
            "service": "Service",
            "configmap": "ConfigMap",
            "statefulset": "StatefulSet",
            "persistentvolumeclaim": "PersistentVolumeClaim",
        }
        wanted = {names[k] for k in kinds.split(",") if k in names}
        return {"items": [o for o in self.desired if o["kind"] in wanted]}

    def read(self, *args):
        return ""  # no chaos kinds

    def delete(self, resource, kind, name, namespace):
        self.ops.append(("delete", kind, name))

    def create(self, objs):
        self.ops += [("create", o["kind"], o["metadata"]["name"]) for o in objs]

    def replace(self, objs):
        self.ops += [("replace", o["kind"], o["metadata"]["name"]) for o in objs]


def _desired():
    values = manifests.template_values(CFG, manifests.image_refs())
    return [
        o for f in manifests.manifest_files("testbed") for o in manifests.load_objects(f, values)
    ]


def test_reset_deletes_stray_config_objects_but_leaves_pods_and_rbac_to_quarantine(
    tmp_path,
) -> None:
    inv = _clean_inventory()
    inv["networkpolicies.networking.k8s.io"] = [_meta("deny-orders")]
    inv["resourcequotas"] = [_meta("quota")]
    inv["pods"].append(_meta("bare-pod"))
    inv["roles.rbac.authorization.k8s.io"].append(_meta("escalate"))
    k = InventoryKubectl(copy.deepcopy(_desired()), inv)
    env = EnvironmentManager(kubectl=k, node=object(), sleep=lambda s: None)
    env.wait_stabilization_window = lambda: True
    env.reset_environment()
    assert ("delete", "NetworkPolicy", "deny-orders") in k.ops
    assert ("delete", "ResourceQuota", "quota") in k.ops
    assert ("delete", "Pod", "bare-pod") not in k.ops
    assert ("delete", "Role", "escalate") not in k.ops


def test_verify_reports_stray_objects(tmp_path) -> None:
    class Node:
        def residue(self):
            return []

    env = EnvironmentManager(node=Node(), sleep=lambda s: None)
    env.wait_stabilization_window = lambda: True
    env.capture_snapshot = load_desired_state
    env._chaos_objects = lambda: []
    env._unexpected = lambda desired=None: [
        ("networkpolicies.networking.k8s.io", "NetworkPolicy", "deny-orders", True)
    ]
    result = env.verify_clean_state()
    assert not result.clean
    assert result.problems == ("NetworkPolicy/deny-orders: unexpected object in the testbed",)


# --- H2: no privileged pods in the testbed or workload namespaces -------------------------------


def test_testbed_and_workload_namespaces_enforce_pod_security_baseline() -> None:
    values = manifests.template_values(CFG, manifests.image_refs())
    ns = {
        o["metadata"]["name"]: o["metadata"].get("labels", {})
        for o in manifests.load_objects(REPO_ROOT / "manifests/system/namespaces.yaml", values)
    }
    for name in (CFG.namespace, CFG.workload_namespace):
        assert ns[name]["pod-security.kubernetes.io/enforce"] == "baseline", name


# --- M1: verify fails closed on anything it can't evaluate ----------------------------------------


def _env_with(capture) -> EnvironmentManager:
    class Node:
        def residue(self):
            return []

    env = EnvironmentManager(node=Node(), sleep=lambda s: None)
    env.wait_stabilization_window = lambda: True
    env.capture_snapshot = capture
    env._chaos_objects = lambda: []
    env._unexpected = lambda desired=None: []
    return env


@pytest.mark.parametrize(
    "exc", [ValueError("Invalid literal for Fraction: '1Pi'"), KeyError("name"), TypeError("x")]
)
def test_verify_fails_closed_on_projection_errors(exc) -> None:
    def broken():
        raise exc

    result = _env_with(broken).verify_clean_state()
    assert not result.clean and "fail closed" in result.problems[0]


def test_verify_still_propagates_guard_errors() -> None:
    def refused():
        raise GuardError("runtime may not mutate cluster-scoped Namespace/x")

    with pytest.raises(GuardError):
        _env_with(refused).verify_clean_state()


# --- M2: a pre-run reset that fails outright quarantines and discards -----------------------------


def test_pre_run_reset_failure_quarantines_and_discards(offline_runner_factory) -> None:
    from safeops.experiment.runner import PreRunDiscard, RunUnit

    r = offline_runner_factory()

    def failing_reset():
        raise CommandError("kubectl delete failed: timed out waiting for the condition")

    r.environment.reset_environment = failing_reset
    with pytest.raises(PreRunDiscard, match="pre-run reset failed"):
        r.run(RunUnit("bad-deployment-config", 0, 0))
    assert "quarantine_and_rebuild" in r.environment.calls


# --- M3: a rebuild that hangs or crashes halts the worker, after the record --------------------


def test_rebuild_timeout_becomes_quarantine_failed(tmp_path, monkeypatch) -> None:
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="safeops.bootstrap down", timeout=1800)

    monkeypatch.setattr(subprocess, "run", hang)
    env = EnvironmentManager()
    with pytest.raises(QuarantineFailed, match="did not finish"):
        env._default_rebuild()


def test_any_rebuild_crash_keeps_the_marker_and_raises_quarantine_failed(tmp_path) -> None:
    import yaml

    from safeops.cluster import parse_cluster_config

    def crash():
        raise RuntimeError("docker daemon went away")

    env = EnvironmentManager(rebuild=crash)
    env._cfg = parse_cluster_config(
        yaml.safe_load((REPO_ROOT / "config/cluster.yaml").read_text()), root=tmp_path
    )
    with pytest.raises(QuarantineFailed, match="RuntimeError"):
        env.quarantine_and_rebuild("x")
    assert env._marker.is_file()


def test_post_run_quarantine_crash_still_lets_the_record_be_written(offline_runner_factory) -> None:
    r = offline_runner_factory()

    def crash(reason=""):
        raise RuntimeError("unexpected")

    r.environment.quarantine_and_rebuild = crash
    assert r._post_run_clean(CleanStateResult(False, ("x",))) is False  # no exception here
    assert isinstance(r._halt, QuarantineFailed)


# --- L1: non-finite numbers don't make a state differ from itself -------------------------------


@pytest.mark.parametrize("raw", ["NaN", "nan", "inf", "-Infinity", "1e999"])
def test_non_finite_config_values_compare_equal_to_themselves(raw) -> None:
    approved = {"max_pool_size": {"hard_range": [1, 100], "safe_range": [5, 30]}}
    cm = {
        "kind": "ConfigMap",
        "metadata": {"name": "db-pool-config"},
        "data": {"max_pool_size": raw},
    }
    a, b = snap.configmap(cm, approved), snap.configmap(copy.deepcopy(cm), approved)
    assert a == b and str(a["numeric"]["max_pool_size"]).startswith("non-finite:")


# --- L2: a halted runner refuses to start another run --------------------------------------------


def test_halted_runner_refuses_to_run(offline_runner_factory) -> None:
    from safeops.experiment.runner import RunUnit

    r = offline_runner_factory()
    r._halt = QuarantineFailed("rebuilt environment is still not clean")
    with pytest.raises(QuarantineFailed):
        r.run(RunUnit("bad-deployment-config", 0, 0))
    assert r.environment.calls == []  # nothing ran


# --- L3: namespaces with no process are inspected too --------------------------------------------


def test_node_check_covers_pinned_network_namespaces() -> None:
    from safeops.environment import node

    assert "/var/run/netns/" in node._QDISC_SCRIPT and "/proc/[0-9]*" in node._QDISC_SCRIPT


# --- L4: a quarantine is lifted only if the environment verifies clean ---------------------------


@pytest.mark.parametrize("clean", [True, False])
def test_clear_quarantine_requires_a_clean_environment(tmp_path, clean) -> None:
    import yaml

    from safeops.cluster import parse_cluster_config

    env = EnvironmentManager()
    env._cfg = parse_cluster_config(
        yaml.safe_load((REPO_ROOT / "config/cluster.yaml").read_text()), root=tmp_path
    )
    env._marker.parent.mkdir(parents=True)
    env._marker.write_text("{}")
    env.verify_clean_state = lambda: CleanStateResult(clean, () if clean else ("still dirty",))
    assert env.clear_quarantine().clean is clean
    assert env._marker.exists() is (not clean)


def test_quarantine_clear_command_exists() -> None:
    from typer.testing import CliRunner

    from safeops.cli import app

    assert "quarantine-clear" in CliRunner().invoke(app, ["--help"]).output


# --- L5: reset always replaces Postgres, so unprojected drift can't persist ----------------------


def test_reset_always_replaces_the_statefulset(tmp_path) -> None:
    k = InventoryKubectl(copy.deepcopy(_desired()), _clean_inventory())
    env = EnvironmentManager(kubectl=k, node=object(), sleep=lambda s: None)
    env.wait_stabilization_window = lambda: True
    env.reset_environment()
    assert ("replace", "StatefulSet", "postgres") in k.ops
    assert ("delete", "StatefulSet", "postgres") not in k.ops
    assert not [o for o in k.ops if o[1] == "PersistentVolumeClaim"]
    _ = Path  # keep the import used
