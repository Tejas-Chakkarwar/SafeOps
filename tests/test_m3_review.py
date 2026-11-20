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
    env = EnvironmentManager(kubectl=k, node=object(), sleep=lambda s: None, image_check=False)
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

    env = EnvironmentManager(node=Node(), sleep=lambda s: None, image_check=False)
    env.wait_stabilization_window = lambda: True
    env.capture_snapshot = load_desired_state
    env._chaos_objects = lambda: []
    env._unexpected = lambda *a: [
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

    env = EnvironmentManager(node=Node(), sleep=lambda s: None, image_check=False)
    env.wait_stabilization_window = lambda: True
    env.capture_snapshot = capture
    env._chaos_objects = lambda: []
    env._unexpected = lambda *a: []
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


def test_post_run_quarantine_crash_becomes_quarantine_failed(offline_runner_factory) -> None:
    r = offline_runner_factory()

    def crash(reason=""):
        raise RuntimeError("unexpected")

    r.environment.quarantine_and_rebuild = crash
    with pytest.raises(QuarantineFailed, match="RuntimeError"):
        r._recover_if_dirty(CleanStateResult(False, ("x",)))
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
    env = EnvironmentManager(kubectl=k, node=object(), sleep=lambda s: None, image_check=False)
    env.wait_stabilization_window = lambda: True
    env.reset_environment()
    assert ("replace", "StatefulSet", "postgres") in k.ops
    assert ("delete", "StatefulSet", "postgres") not in k.ops
    assert not [o for o in k.ops if o[1] == "PersistentVolumeClaim"]
    _ = Path  # keep the import used


# ============================== second review ==============================================

# --- R1: configuration errors raise; they're never reported as a dirty environment -------------


def test_config_errors_raise_instead_of_looking_dirty(monkeypatch) -> None:
    from safeops.environment import desired as desired_mod
    from safeops.environment.manager import ConfigurationError

    def stale(*a, **k):
        raise ValueError("desired_state.yaml was generated by a different projection version")

    monkeypatch.setattr(desired_mod, "load_desired_state", stale)
    env = _env_with(load_desired_state)
    with pytest.raises(ConfigurationError, match="projection version"):
        env.verify_clean_state()
    with pytest.raises(ConfigurationError):
        env.reset_environment()


# --- R2: any post-run reset failure keeps the run; the record is written first -----------------


def test_post_run_reset_failure_of_any_kind_still_writes_the_record(offline_runner_factory) -> None:
    from safeops.cluster import CredentialError
    from safeops.experiment.runner import ExperimentRunner, RunUnit

    finalized = {}

    class Records:
        def finalize(self, **fields):
            finalized.update(fields)

    class Anything:
        def __getattr__(self, name):
            return lambda *a, **k: (object(), object()) if name == "investigate" else object()

    base = offline_runner_factory()
    resets = {"n": 0}

    def reset():
        resets["n"] += 1
        if resets["n"] == 2:  # the post-run reset
            raise CredentialError("harness token has expired")

    base.environment.reset_environment = reset

    class Through(ExperimentRunner):  # take the full path past the still-stubbed stages
        def _require_fault_present(self, present):
            pass

        def _runtime_context(self, alert):
            return object()

        def _gate_eligible_action(self, proposal):
            return None

    r = Through(
        environment=base.environment,
        workload=base.workload,
        injector=Anything(),
        alerts=Anything(),
        agent=Anything(),
        parser=Anything(),
        extractor=Anything(),
        shadow_gates=Anything(),
        gate=Anything(),
        executor=Anything(),
        effectiveness=Anything(),
        harm=Anything(),
        records=Records(),
    )
    r.run(RunUnit("bad-deployment-config", 0, 0))
    assert finalized["reset_verified"] is False
    assert "quarantine_and_rebuild" in base.environment.calls


# --- R3: inert kinds can't hide ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "kind"),
    [
        ("daemonsets.apps", "DaemonSet"),
        ("replicationcontrollers", "ReplicationController"),
        ("podtemplates", "PodTemplate"),
        ("leases.coordination.k8s.io", "Lease"),
    ],
)
def test_inert_kinds_are_inventoried(resource, kind) -> None:
    items = _clean_inventory()
    items[resource] = [_meta("inert")]
    assert unexpected_objects(items, DEPLOYMENTS, STATEFULSETS, RBAC) == [
        (resource, kind, "inert", True)
    ]


# --- R4: images reset deploys must be the ones kind has --------------------------------------


def _image_env(tmp_path, built):
    import json

    import yaml

    from safeops.cluster import parse_cluster_config

    env = EnvironmentManager()
    env._cfg = parse_cluster_config(
        yaml.safe_load((REPO_ROOT / "config/cluster.yaml").read_text()), root=tmp_path
    )
    env._cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if built is not None:
        (env._cfg.state_dir / "images.json").write_text(json.dumps({"built": built}))
    return env


def test_image_check_passes_when_kind_has_the_source_images(tmp_path) -> None:
    refs = manifests.image_refs()
    _image_env(tmp_path, {k: {"ref": v} for k, v in refs.items()})._check_images_loaded()


@pytest.mark.parametrize(
    "built",
    [
        None,
        {
            "testbed": {"ref": "safeops-testbed:000000000000"},
            "loadgen": {"ref": "safeops-loadgen:000000000000"},
        },
    ],
)
def test_image_check_explains_a_stale_or_missing_build(tmp_path, built) -> None:
    from safeops.environment.manager import ConfigurationError

    with pytest.raises(ConfigurationError, match="cluster-up"):
        _image_env(tmp_path, built)._check_images_loaded()


# --- R5: hand-made EndpointSlices are strays, and don't stall verify --------------------------


def _slice(name, managed=True, owner="orders-svc", ready=1):
    m = {"name": name, "labels": {"kubernetes.io/service-name": owner}}
    if managed:
        m["labels"]["endpointslice.kubernetes.io/managed-by"] = "endpointslice-controller.k8s.io"
        m["ownerReferences"] = [{"kind": "Service", "name": owner, "controller": True}]
    return {"metadata": m, "endpoints": [{"conditions": {"ready": True}}] * ready}


def test_controller_slices_of_desired_services_are_expected() -> None:
    items = _clean_inventory()
    items["endpointslices.discovery.k8s.io"] = [_slice("orders-svc-abc")]
    assert unexpected_objects(items, DEPLOYMENTS, STATEFULSETS, RBAC, {"orders-svc"}) == []


@pytest.mark.parametrize(
    "bad", [_slice("inject", managed=False), _slice("ghost-svc-abc", owner="ghost-svc")]
)
def test_hand_made_or_foreign_slices_are_strays(bad) -> None:
    items = _clean_inventory()
    items["endpointslices.discovery.k8s.io"] = [bad]
    assert unexpected_objects(items, DEPLOYMENTS, STATEFULSETS, RBAC, {"orders-svc"}) == [
        ("endpointslices.discovery.k8s.io", "EndpointSlice", bad["metadata"]["name"], True)
    ]


def test_a_hand_made_slice_does_not_make_the_testbed_look_unstable() -> None:
    from safeops.environment.manager import stability_problems

    svc = {"metadata": {"name": "orders-svc"}, "spec": {"selector": {"app": "orders"}}}
    pod = {
        "metadata": {"name": "orders-1", "labels": {"app": "orders"}},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    slices = [_slice("orders-svc-abc"), _slice("inject", managed=False)]
    assert stability_problems([], [], [svc], slices, [pod]) == []


# ============================== third review ===============================================

# --- finalize strictly before recovery; workload stop is cleanup ------------------------------


class _Events:
    def __init__(self):
        self.log: list[str] = []


def _full_runner(
    events: _Events,
    *,
    verify_dirty=False,
    stop_raises=False,
    reset_raises=False,
    quarantine_fails=False,
):
    """A real ExperimentRunner driven end to end: every stage after the pre-run phase is
    a double, the still-stubbed hooks take the full path, and a shared event log records
    the order of the post-measurement steps."""
    from tests.conftest import FakeEnvironment, FakeWorkload  # noqa: I001

    from safeops.experiment.runner import ExperimentRunner

    class Env(FakeEnvironment):
        def __init__(self):
            super().__init__()
            self.resets = 0

        def reset_environment(self):
            self.resets += 1
            if self.resets == 2:
                events.log.append("reset")
                if reset_raises:
                    raise CommandError("kubectl delete failed: timed out")

        def verify_clean_state(self):
            if self.resets >= 2:
                events.log.append("verify")
                if verify_dirty:
                    return CleanStateResult(False, ("Deployment/orders: drift",))
            return CleanStateResult(True, ())

        def quarantine_and_rebuild(self, reason=""):
            events.log.append("quarantine")
            if quarantine_fails:
                raise QuarantineFailed("rebuilt environment is still not clean")

    class Workload(FakeWorkload):
        def stop_workload(self):
            events.log.append("stop_workload")
            if stop_raises:
                raise CommandError("kubectl replace failed: generator unreachable")

    class Records:
        def __init__(self):
            self.fields = None

        def finalize(self, **fields):
            events.log.append("finalize")
            self.fields = fields

    class Anything:
        def __getattr__(self, name):
            def call(*a, **k):
                if name == "check":
                    events.log.append("harm.check")
                return (object(), object()) if name == "investigate" else object()

            return call

    class Through(ExperimentRunner):
        def _require_fault_present(self, present):
            pass

        def _runtime_context(self, alert):
            return object()

        def _gate_eligible_action(self, proposal):
            return None

    records = Records()
    r = Through(
        environment=Env(),
        workload=Workload(),
        injector=Anything(),
        alerts=Anything(),
        agent=Anything(),
        parser=Anything(),
        extractor=Anything(),
        shadow_gates=Anything(),
        gate=Anything(),
        executor=Anything(),
        effectiveness=Anything(),
        harm=Anything(),
        records=records,
    )
    return r, records


def _run(r):
    from safeops.experiment.runner import RunUnit

    r.run(RunUnit("bad-deployment-config", 0, 0))


def test_dirty_post_run_finalizes_before_quarantine() -> None:
    ev = _Events()
    r, records = _full_runner(ev, verify_dirty=True)
    _run(r)
    assert ev.log.index("finalize") < ev.log.index("quarantine")
    assert ev.log[-2:] == ["finalize", "quarantine"]
    assert records.fields["reset_verified"] is False


def test_stop_workload_failure_keeps_the_run_and_recovers_after_the_record() -> None:
    ev = _Events()
    r, records = _full_runner(ev, stop_raises=True)
    _run(r)  # no exception escapes: the completed measurements are kept
    assert ev.log.index("harm.check") < ev.log.index("stop_workload")
    assert ev.log.index("finalize") < ev.log.index("quarantine")
    assert records.fields["reset_verified"] is False  # cleanup failed, even though verify was clean


def test_post_run_reset_exception_finalizes_before_quarantine() -> None:
    ev = _Events()
    r, records = _full_runner(ev, reset_raises=True)
    _run(r)
    assert ev.log.index("finalize") < ev.log.index("quarantine")
    assert records.fields["reset_verified"] is False


def test_failed_recovery_halts_only_after_the_record() -> None:
    ev = _Events()
    r, records = _full_runner(ev, verify_dirty=True, quarantine_fails=True)
    with pytest.raises(QuarantineFailed):
        _run(r)
    assert ev.log.index("finalize") < ev.log.index("quarantine")
    assert records.fields is not None  # the record exists although the worker halted
    with pytest.raises(QuarantineFailed):  # and the halted runner refuses further runs
        _run(r)


def test_clean_post_run_does_not_quarantine() -> None:
    ev = _Events()
    r, records = _full_runner(ev)
    _run(r)
    assert "quarantine" not in ev.log and records.fields["reset_verified"] is True


# --- a valid but stale desired_state.yaml is a configuration error ---------------------------


def test_stale_desired_state_raises_configuration_error_for_reset_and_verify(monkeypatch) -> None:
    from safeops.environment import desired as desired_mod
    from safeops.environment.manager import ConfigurationError

    stale = load_desired_state()  # valid, current projection version...
    stale.resources[("Deployment", "orders")]["replicas"] = 3  # ...but not what manifests say
    monkeypatch.setattr(desired_mod, "load_desired_state", lambda *a, **k: stale)

    class NeverCalled:
        def __getattr__(self, name):
            raise AssertionError(f"cluster touched ({name}) despite stale configuration")

    env = EnvironmentManager(kubectl=NeverCalled(), node=NeverCalled(), image_check=False)
    with pytest.raises(ConfigurationError, match="stale; run make desired-state"):
        env.verify_clean_state()
    with pytest.raises(ConfigurationError, match="stale; run make desired-state"):
        env.reset_environment()


def test_committed_desired_state_is_current() -> None:
    from safeops.environment.desired import desired_snapshot

    assert snap.diff(load_desired_state(), desired_snapshot()) == []


# --- probes: every behavior-relevant field is projected --------------------------------------


def _probe_deployment(**probe_overrides):
    values = manifests.template_values(CFG, manifests.image_refs())
    objs = {
        o["metadata"]["name"]: o
        for f in manifests.manifest_files("testbed")
        for o in manifests.load_objects(f, values)
        if o["kind"] == "Deployment"
    }
    d = copy.deepcopy(objs["orders"])
    probe = d["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    for key, value in probe_overrides.items():
        if key == "httpHeaders":
            probe["httpGet"]["httpHeaders"] = value
        else:
            probe[key] = value
    return d


def test_http_probe_header_change_is_a_snapshot_diff() -> None:
    a = _probe_deployment(httpHeaders=[{"name": "X-Probe", "value": "a"}])
    b = _probe_deployment(httpHeaders=[{"name": "X-Probe", "value": "b"}])
    assert snap.deployment(a) != snap.deployment(b)
    assert snap.deployment(_probe_deployment()) != snap.deployment(a)  # adding a header too


def test_http_probe_header_canonicalization_is_stable() -> None:
    a = _probe_deployment(
        httpHeaders=[{"name": "X-A", "value": "1"}, {"name": "x-b", "value": "2"}]
    )
    b = _probe_deployment(
        httpHeaders=[{"name": "X-B", "value": "2"}, {"name": "x-a", "value": "1"}]
    )
    assert snap.deployment(a) == snap.deployment(b)  # order and name case don't matter
    c = _probe_deployment(
        httpHeaders=[{"name": "X-A", "value": "1"}, {"name": "X-A", "value": "1"}]
    )
    d = _probe_deployment(httpHeaders=[{"name": "X-A", "value": "1"}])
    assert snap.deployment(c) != snap.deployment(d)  # a repeated header is meaningful


@pytest.mark.parametrize(
    "override",
    [
        {"terminationGracePeriodSeconds": 5},
        {"periodSeconds": 3},
        {"timeoutSeconds": 4},
        {"failureThreshold": 1},
        {"successThreshold": 2},
        {"initialDelaySeconds": 9},
    ],
)
def test_every_probe_timing_field_is_projected(override) -> None:
    assert snap.deployment(_probe_deployment(**override)) != snap.deployment(_probe_deployment())


def test_startup_probe_is_projected() -> None:
    a = _probe_deployment()
    b = copy.deepcopy(a)
    b["spec"]["template"]["spec"]["containers"][0]["startupProbe"] = {
        "httpGet": {"path": "/healthz", "port": "http"},
        "failureThreshold": 1,
    }
    assert snap.deployment(a) != snap.deployment(b)
