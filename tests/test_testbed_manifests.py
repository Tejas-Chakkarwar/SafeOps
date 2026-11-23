"""Testbed manifests against the topology and the decisions, checked offline.

- Every projected testbed object maps to config/topology.yaml, and back.
- Requests, limits and replicas sit inside each Deployment's safe_range (0003 F4).
- Every approved ConfigMap key is actually consumed by its owner, so patch_configmap plus
  restart can repair it (0007 config split).
- Images: one shared testbed image, a separate loadgen image, IfNotPresent pulls (0007).
- The workload generator is outside the testbed namespace (0009).
"""

from __future__ import annotations

import re

import pytest

from safeops import manifests
from safeops.cluster import load_cluster_config
from safeops.topology import PROJECTED_KINDS, load_topology, ownership_report

CFG = load_cluster_config()
TOPO = load_topology()
IMAGES = {"testbed": "safeops-testbed:test", "loadgen": "safeops-loadgen:test"}
VALUES = manifests.template_values(CFG, IMAGES)
GROUPS = {
    g: [o for f in manifests.manifest_files(g) for o in manifests.load_objects(f, VALUES)]
    for g in ("system", "testbed", "workload")
}
TESTBED = GROUPS["testbed"]
DEPLOYMENTS = {o["metadata"]["name"]: o for o in TESTBED if o["kind"] == "Deployment"}


def _mib(q: str) -> float:
    m = re.fullmatch(r"(\d+)(Mi|Gi)", q)
    assert m, f"unsupported memory quantity {q}"
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


def _millis(q: str) -> float:
    return int(q[:-1]) if q.endswith("m") else float(q) * 1000


def _live_view() -> list[dict]:
    """What the cluster will hold: manifests plus the StatefulSet's PVC and the Secret."""
    items = [o for o in TESTBED if o["kind"] in PROJECTED_KINDS]
    for sts in (o for o in TESTBED if o["kind"] == "StatefulSet"):
        for tpl in sts["spec"].get("volumeClaimTemplates", []):
            for i in range(sts["spec"]["replicas"]):
                name = f"{tpl['metadata']['name']}-{sts['metadata']['name']}-{i}"
                items.append({"kind": "PersistentVolumeClaim", "metadata": {"name": name}})
    return items


def test_rendering_is_strict() -> None:
    with pytest.raises(KeyError):
        manifests.render("x: ${NOT_A_PLACEHOLDER}", VALUES)
    assert manifests.render("replacement: $1:$2", VALUES) == "replacement: $1:$2"


def test_no_unrendered_placeholders_remain() -> None:
    for group in GROUPS.values():
        assert "${" not in repr(group)


def test_ownership_map_resolves_every_testbed_object_both_ways() -> None:
    assert ownership_report(TOPO, _live_view()) == []


def test_every_testbed_object_is_in_the_testbed_namespace() -> None:
    for obj in TESTBED:
        assert obj["metadata"]["namespace"] == CFG.namespace, obj["metadata"]["name"]


@pytest.mark.parametrize("name", ["gateway", "orders", "inventory", "payment"])
def test_replicas_and_resources_inside_safe_range(name: str) -> None:
    dep, spec = DEPLOYMENTS[name], TOPO.resources[name]
    assert spec.bounds["replicas"].safe.contains(dep["spec"]["replicas"])
    for c in dep["spec"]["template"]["spec"]["containers"]:
        for section in ("requests", "limits"):
            res = c["resources"][section]
            assert spec.bounds["memory_mib"].safe.contains(_mib(res["memory"])), (name, section)
            assert spec.bounds["cpu_millis"].safe.contains(_millis(res["cpu"])), (name, section)


@pytest.mark.parametrize("name", ["gateway", "orders", "inventory", "payment"])
def test_testbed_deployments_share_the_testbed_image_with_ifnotpresent(name: str) -> None:
    for c in DEPLOYMENTS[name]["spec"]["template"]["spec"]["containers"]:
        assert c["image"] == IMAGES["testbed"]
        # Never: a bad tag would give ErrImageNeverPull instead of ImagePullBackOff (§9.6).
        assert c["imagePullPolicy"] == "IfNotPresent"


def test_every_approved_configmap_key_is_consumed_by_its_owner() -> None:
    for spec in TOPO.resources.values():
        if spec.kind != "ConfigMap":
            continue
        owner = DEPLOYMENTS[spec.owner]
        refs = {
            (e["valueFrom"]["configMapKeyRef"]["name"], e["valueFrom"]["configMapKeyRef"]["key"])
            for c in owner["spec"]["template"]["spec"]["containers"]
            for e in c.get("env", [])
            if "configMapKeyRef" in e.get("valueFrom", {})
        }
        for key in spec.approved_keys:
            assert (spec.name, key) in refs, f"{spec.name}.{key} is not consumed by {spec.owner}"


def test_configmap_values_start_inside_safe_range() -> None:
    cms = {o["metadata"]["name"]: o for o in TESTBED if o["kind"] == "ConfigMap"}
    for spec in TOPO.resources.values():
        if spec.kind != "ConfigMap":
            continue
        for key, rule in spec.approved_keys.items():
            value = cms[spec.name]["data"][key]
            if "enum" in rule:
                assert value in rule["enum"]
            else:
                lo, hi = rule["safe_range"]
                assert lo <= float(value) <= hi, (spec.name, key)


def test_rollback_repairable_values_live_in_the_pod_template() -> None:
    """URLs and timeouts are plain pod-template env, so rollback_deployment restores them."""
    orders_env = {
        e["name"]: e
        for c in DEPLOYMENTS["orders"]["spec"]["template"]["spec"]["containers"]
        for e in c["env"]
    }
    for name in ("INVENTORY_URL", "PAYMENT_URL", "UPSTREAM_TIMEOUT_MS", "CACHE_MB"):
        assert "value" in orders_env[name] and "valueFrom" not in orders_env[name]


def test_every_testbed_app_pod_is_scraped() -> None:
    for dep in DEPLOYMENTS.values():
        ann = dep["spec"]["template"]["metadata"]["annotations"]
        assert ann["prometheus.io/scrape"] == "true"


def test_workload_generator_is_outside_the_testbed_and_uses_its_own_image() -> None:
    wl = GROUPS["workload"]
    assert {o["metadata"]["namespace"] for o in wl} == {CFG.workload_namespace}
    dep = next(o for o in wl if o["kind"] == "Deployment")
    assert dep["metadata"]["name"] == CFG.workload_service
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGES["loadgen"]
    svc = next(o for o in wl if o["kind"] == "Service")
    assert svc["metadata"]["name"] == CFG.workload_service
    # One unnamed port: the proxy path then uses the bare Service name, which is exactly
    # the RBAC resourceName. With a named port the bare-name proxy returns
    # "no endpoints available" (found live on 2026-11-19, decision 0009).
    assert len(svc["spec"]["ports"]) == 1 and "name" not in svc["spec"]["ports"][0]


def test_experiment_controller_rbac_is_exactly_proxy_get_update_by_name() -> None:
    system = GROUPS["system"]
    sa = next(
        o
        for o in system
        if o["kind"] == "ServiceAccount" and o["metadata"]["name"] == "experiment-controller"
    )
    assert sa["metadata"]["namespace"] == CFG.system_namespace
    # safeops-reader's roles (Milestone 6) are checked in tests/test_m6_tools.py.
    roles = {
        o["metadata"]["name"]: o
        for o in system
        if o["kind"] in ("Role", "ClusterRole")
        and o["metadata"]["name"].startswith("experiment-controller")
    }
    assert {r["kind"] for r in roles.values()} == {"Role"}  # namespaced only, never cluster-wide
    proxy = roles["experiment-controller-workload-proxy"]
    assert proxy["metadata"]["namespace"] == CFG.workload_namespace
    assert proxy["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["services/proxy"],
            "resourceNames": [CFG.workload_service],
            "verbs": ["get", "update"],
        }
    ]
    lifecycle = roles["experiment-controller-testbed-lifecycle"]  # Milestone 3 (decision 0013)
    assert lifecycle["metadata"]["namespace"] == CFG.namespace
    granted = {
        (g, r, v)
        for rule in lifecycle["rules"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }
    assert not [
        x for x in granted if "secrets" in x[1] or "/" in x[1]
    ]  # no Secrets, no subresources
    assert ("", "endpoints", "create") not in granted  # inventory + stray removal only (0021)
    assert ("", "endpoints", "list") in granted and ("", "endpoints", "delete") in granted
    assert ("", "pods", "delete") not in granted and (
        "chaos-mesh.org",
        "*",
        "create",
    ) not in granted
    assert len(roles) == 2
    assert not [o for o in system if o["kind"] in ("ClusterRole", "ClusterRoleBinding")]


def test_image_tags_are_content_derived_and_separate() -> None:
    refs = manifests.image_refs()
    assert refs["testbed"].startswith("safeops-testbed:")
    assert refs["loadgen"].startswith("safeops-loadgen:")
    assert "app/loadgen.py" not in manifests.IMAGE_INPUTS["testbed"]
    assert "app/window.py" not in manifests.IMAGE_INPUTS["testbed"]
    assert "app/service.py" not in manifests.IMAGE_INPUTS["loadgen"]


def test_dockerfile_keeps_measurement_code_out_of_the_testbed_stage() -> None:
    text = (manifests.TESTBED_SRC / "Dockerfile").read_text()
    stages = re.split(r"(?m)^FROM ", text)
    testbed_stage = next(s for s in stages if " AS testbed" in s.splitlines()[0])
    base_stage = next(s for s in stages if " AS base" in s.splitlines()[0])
    for stage in (testbed_stage, base_stage):
        assert "loadgen.py" not in stage and "window.py" not in stage
    assert "@sha256:" in base_stage.splitlines()[0]  # base image pinned by digest
