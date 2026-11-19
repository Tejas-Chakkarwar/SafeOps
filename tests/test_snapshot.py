"""Canonical snapshots (§11.1, decisions 0011, 0013): hand-written, so tested against
fabricated objects. A controller's churn must never look like a change, and a real
change must never be lost.
"""

from __future__ import annotations

import copy

import pytest

from safeops import manifests
from safeops.cluster import load_cluster_config
from safeops.environment import snapshot as snap
from safeops.environment.desired import (
    DESIRED_STATE_PATH,
    desired_snapshot,
    load_desired_state,
    render,
)
from safeops.topology import load_topology

CFG, TOPO = load_cluster_config(), load_topology()
VALUES = manifests.template_values(CFG, manifests.image_refs())
MANIFEST = {
    (o["kind"], o["metadata"]["name"]): o
    for f in manifests.manifest_files("testbed")
    for o in manifests.load_objects(f, VALUES)
}


def _as_live(obj: dict) -> dict:
    """What the API server hands back: defaults filled in, plus churn the snapshot must ignore."""
    live = copy.deepcopy(obj)
    meta = live["metadata"]
    meta.update(
        uid="b2c1",
        resourceVersion="98765",
        generation=7,
        creationTimestamp="2026-11-19T08:00:00Z",
        managedFields=[{"manager": "kubectl", "operation": "Update"}],
        annotations={"deployment.kubernetes.io/revision": "3"},
    )
    if live["kind"] in ("Deployment", "StatefulSet"):
        tpl = live["spec"]["template"]
        tpl["metadata"].setdefault("annotations", {})["kubectl.kubernetes.io/restartedAt"] = "now"
        for c in tpl["spec"]["containers"]:
            c.update(terminationMessagePath="/dev/termination-log", imagePullPolicy="IfNotPresent")
            for probe in ("readinessProbe", "livenessProbe"):
                if probe in c:
                    p = c[probe]
                    p.update({k: v for k, v in snap._PROBE_DEFAULTS.items() if k not in p})
                    if "httpGet" in p:
                        p["httpGet"].setdefault("scheme", "HTTP")
            # the API server normalizes quantities; "100m" stays, "256Mi" stays, but test
            # the equivalent forms too
            res = c.get("resources", {})
            if res.get("requests", {}).get("cpu") == "100m":
                res["requests"]["cpu"] = "0.1"
        live["status"] = {"replicas": 2, "readyReplicas": 2, "observedGeneration": 7}
        live["spec"]["revisionHistoryLimit"] = 10
    if live["kind"] == "Service":
        live["spec"].update(
            clusterIP=live["spec"].get("clusterIP", "10.96.12.34"),
            sessionAffinity="None",
            ipFamilies=["IPv4"],
        )
        for p in live["spec"]["ports"]:
            p.setdefault("protocol", "TCP")
    return live


# --- quantities ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("q", "want"),
    [("100m", 100), ("0.1", 100), ("1", 1000), ("1500m", 1500), ("2", 2000), ("0.25", 250)],
)
def test_cpu_millis(q, want) -> None:
    assert snap.cpu_millis(q) == want


@pytest.mark.parametrize(
    ("q", "want"),
    [
        ("256Mi", 256),
        ("1Gi", 1024),
        ("268435456", 256),
        ("262144Ki", 256),
        ("512Mi", 512),
        ("1G", 953.674316),
    ],
)
def test_memory_mib(q, want) -> None:
    assert snap.memory_mib(q) == want


# --- churn is invisible --------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        ("Deployment", "orders"),
        ("Deployment", "gateway"),
        ("StatefulSet", "postgres"),
        ("ConfigMap", "db-pool-config"),
    ],
)
def test_live_object_projects_exactly_like_its_manifest(key) -> None:
    manifest, live = MANIFEST[key], _as_live(MANIFEST[key])
    if key[0] == "Deployment":
        assert snap.deployment(live) == snap.deployment(manifest)
    elif key[0] == "StatefulSet":
        assert snap.statefulset(live) == snap.statefulset(manifest)
    else:
        approved = TOPO.resource("ConfigMap", key[1]).approved_keys
        assert snap.configmap(live, approved) == snap.configmap(manifest, approved)


def test_restart_annotation_is_not_a_change() -> None:
    a = copy.deepcopy(MANIFEST[("Deployment", "orders")])
    b = copy.deepcopy(a)
    b["spec"]["template"]["metadata"]["annotations"] = {"kubectl.kubernetes.io/restartedAt": "t"}
    assert snap.deployment(a) == snap.deployment(b)


def test_service_cluster_ip_is_not_a_change_but_headless_is() -> None:
    svc = MANIFEST[("Service", "orders-svc")]
    a, b = copy.deepcopy(svc), copy.deepcopy(svc)
    a["spec"]["clusterIP"], b["spec"]["clusterIP"] = "10.96.0.10", "10.96.0.99"
    assert snap.service(a, 2, ["orders"]) == snap.service(b, 2, ["orders"])
    b["spec"]["clusterIP"] = "None"
    assert snap.service(a, 2, ["orders"]) != snap.service(b, 2, ["orders"])


# --- real changes are visible ----------------------------------------------------------------


def _orders():
    return copy.deepcopy(MANIFEST[("Deployment", "orders")])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["spec"].update(replicas=5),
        lambda d: d["spec"]["template"]["spec"]["containers"][0].update(
            image="safeops-testbed:bad"
        ),
        lambda d: d["spec"]["template"]["spec"]["containers"][0].pop("readinessProbe"),
        lambda d: d["spec"]["template"]["spec"]["containers"][0]["env"][0].update(value="other"),
        lambda d: d["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"].update(
            memory="64Mi"
        ),
        lambda d: d["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"].update(
            cpu="50m"
        ),
    ],
    ids=["replicas", "image", "probe-removed", "env", "limit", "request"],
)
def test_projected_changes_are_visible(mutate) -> None:
    d = _orders()
    mutate(d)
    assert snap.deployment(d) != snap.deployment(_orders())


def test_request_and_limit_stay_separate() -> None:
    """decision 0011: never collapsed into one value."""
    res = snap.deployment(_orders())["containers"][0]["resources"]
    assert res == {
        "requests": {"cpu_millis": 100, "memory_mib": 256},
        "limits": {"cpu_millis": 500, "memory_mib": 512},
    }


def test_configmap_approved_keys_numeric_and_hash() -> None:
    approved = TOPO.resource("ConfigMap", "db-pool-config").approved_keys
    base = MANIFEST[("ConfigMap", "db-pool-config")]
    changed = copy.deepcopy(base)
    changed["data"]["max_pool_size"] = "90"
    a, b = snap.configmap(base, approved), snap.configmap(changed, approved)
    assert a["numeric"]["max_pool_size"] == 10.0 and b["numeric"]["max_pool_size"] == 90.0
    assert a["approved_keys_sha256"] != b["approved_keys_sha256"]
    unrelated = copy.deepcopy(base)
    unrelated["data"]["not_approved"] = "x"
    assert snap.configmap(unrelated, approved) == a  # only approved keys are projected
    garbage = copy.deepcopy(base)
    garbage["data"]["max_pool_size"] = "lots"
    assert snap.configmap(garbage, approved)["numeric"]["max_pool_size"] == "unparseable:lots"


def test_unknown_configmap_is_hashed_in_full() -> None:
    a = {"kind": "ConfigMap", "metadata": {"name": "rogue"}, "data": {"k": "1"}}
    b = copy.deepcopy(a)
    b["data"]["k"] = "2"
    assert snap.configmap(a, None) != snap.configmap(b, None)


# --- endpoints and ownership -------------------------------------------------------------------


def _pod(name, rs=None, sts=None, ready=True):
    ref = (
        {"kind": "ReplicaSet", "name": rs, "controller": True}
        if rs
        else {"kind": "StatefulSet", "name": sts, "controller": True}
    )
    return {
        "metadata": {"name": name, "ownerReferences": [ref], "labels": {"app": "orders"}},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def _rs(name, deployment):
    return {
        "metadata": {
            "name": name,
            "ownerReferences": [{"kind": "Deployment", "name": deployment, "controller": True}],
        }
    }


def _slice(service, endpoints):
    return {
        "metadata": {"labels": {"kubernetes.io/service-name": service}},
        "endpoints": [
            {"conditions": {"ready": r}, "targetRef": {"kind": "Pod", "name": n}}
            for n, r in endpoints
        ],
    }


def test_endpoint_owners_come_from_owner_references_not_labels() -> None:
    """orders-svc repointed at inventory pods: plenty of ready endpoints, wrong owner (§11.7)."""
    pods = [_pod("inv-1", rs="inventory-abc"), _pod("inv-2", rs="inventory-abc")]  # labels lie
    rss = [_rs("inventory-abc", "inventory")]

    def owner_of(kind, name):
        return TOPO.resource(kind, name).owner

    owners = snap.pod_owners(pods, rss, owner_of)
    ready, owner_list = snap.endpoints_for(
        "orders-svc", [_slice("orders-svc", [("inv-1", True), ("inv-2", True)])], owners
    )
    assert (ready, owner_list) == (2, ["inventory"])


def test_only_ready_endpoints_count() -> None:
    ready, _ = snap.endpoints_for("s", [_slice("s", [("a", True), ("b", False), ("c", None)])], {})
    assert ready == 1


# --- snapshot, diff, desired state ------------------------------------------------------------


def test_diff_reports_missing_unexpected_and_changed() -> None:
    a = snap.Snapshot({("Deployment", "orders"): {"replicas": 2}, ("Service", "x"): {"p": 1}})
    b = snap.Snapshot({("Deployment", "orders"): {"replicas": 0}, ("ConfigMap", "rogue"): {}})
    assert snap.diff(a, b) == [
        "ConfigMap/rogue: unexpected",
        "Deployment/orders.replicas: expected 2, got 0",
        "Service/x: missing",
    ]
    assert snap.diff(a, a) == []


def test_snapshot_json_roundtrip_and_digest() -> None:
    s = desired_snapshot()
    again = snap.Snapshot.from_json(s.to_json())
    assert snap.diff(s, again) == [] and again.digest() == s.digest()


def test_desired_state_file_matches_manifests() -> None:
    """Editing manifests/testbed or the topology without `make desired-state` fails here."""
    assert DESIRED_STATE_PATH.read_text() == render(), "run `make desired-state`"


def test_desired_state_has_every_topology_resource() -> None:
    desired = load_desired_state()
    assert set(desired.resources) == {(s.kind, s.name) for s in TOPO.resources.values()}
    assert desired.resources[("Service", "orders-svc")]["ready_endpoint_count"] == 2
    assert desired.resources[("Service", "orders-svc")]["endpoint_owner_services"] == ["orders"]
    assert desired.resources[("Service", "postgres-svc")]["headless"] is True
    assert desired.resources[("PersistentVolumeClaim", "data-postgres-0")] == {"bound": True}


def test_build_ignores_unprojected_and_auto_created_objects() -> None:
    objs = [
        _as_live(MANIFEST[("ConfigMap", "orders-config")]),
        {"kind": "ConfigMap", "metadata": {"name": "kube-root-ca.crt"}, "data": {}},
        {"kind": "Secret", "metadata": {"name": "postgres-credentials"}},
        {"kind": "ReplicaSet", "metadata": {"name": "orders-abc"}},
        {"kind": "Pod", "metadata": {"name": "orders-abc-1"}},
    ]
    built = snap.build(objs, [], [], [], TOPO)
    assert set(built.resources) == {("ConfigMap", "orders-config")}
