"""Topology: §12 graph-direction tables, validation, and ownership resolution."""

from __future__ import annotations

import copy

import pytest

from safeops.topology import TopologyError, load_topology, ownership_report, parse_topology

TOPO = load_topology()


# design §12, "Graph direction" tables, verbatim.
@pytest.mark.parametrize(
    ("alert_target", "scope"),
    [
        ("gateway", {"gateway", "orders", "inventory", "payment", "postgres"}),
        ("orders", {"orders", "inventory", "payment", "postgres"}),
        ("inventory", {"inventory"}),
    ],
)
def test_incident_scope_matches_design_table(alert_target: str, scope: set[str]) -> None:
    assert TOPO.incident_scope(alert_target) == scope


@pytest.mark.parametrize(
    ("target", "radius"),
    [
        ("gateway", {"gateway"}),
        ("orders", {"orders", "gateway"}),
        ("postgres", {"postgres", "orders", "gateway"}),
        ("inventory", {"inventory", "orders", "gateway"}),
    ],
)
def test_blast_radius_matches_design_table(target: str, radius: set[str]) -> None:
    assert TOPO.blast_radius_set(target) == radius


def test_gateway_is_out_of_scope_for_an_orders_alert() -> None:
    assert "gateway" not in TOPO.incident_scope("orders")
    assert "inventory" in TOPO.incident_scope("orders")  # declared dependency (§11.7)


def test_every_resource_owner_and_kind() -> None:
    owners = {name: (spec.kind, spec.owner) for name, spec in TOPO.resources.items()}
    assert owners["orders-config"] == ("ConfigMap", "orders")
    assert owners["db-pool-config"] == ("ConfigMap", "orders")
    assert owners["postgres"] == ("StatefulSet", "postgres")
    assert owners["data-postgres-0"] == ("PersistentVolumeClaim", "postgres")
    for svc in ("gateway", "orders", "inventory", "payment"):
        assert owners[svc] == ("Deployment", svc)
        assert owners[f"{svc}-svc"] == ("Service", svc)
    assert owners["postgres-svc"] == ("Service", "postgres")


def test_every_deployment_has_hard_and_safe_ranges() -> None:
    for spec in TOPO.resources.values():
        if spec.kind == "Deployment":
            assert set(spec.bounds) == {"replicas", "memory_mib", "cpu_millis"}
            for b in spec.bounds.values():
                assert b.hard.lo <= b.safe.lo <= b.safe.hi <= b.hard.hi


def test_db_pool_config_numeric_keys_have_both_ranges() -> None:
    keys = TOPO.resources["db-pool-config"].approved_keys
    assert keys["max_pool_size"] == {"hard_range": [1, 100], "safe_range": [5, 30]}
    assert TOPO.resources["db-pool-config"].persistence_affecting is True


def _raw() -> dict:
    import yaml

    from safeops.topology import TOPOLOGY_PATH

    return yaml.safe_load(TOPOLOGY_PATH.read_text())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["services"]["orders"]["depends_on"].append("nowhere"),
        lambda d: d["resources"]["orders"].update(owner="nobody"),
        lambda d: d["resources"]["orders"]["replicas"].update(safe_range=[2, 30]),  # safe > hard
        lambda d: d["resources"]["orders"]["replicas"].update(safe_range=[6, 2]),
        lambda d: d["resources"]["orders"].pop("cpu_millis"),
        lambda d: d["resources"]["orders"].update(kind="Secret"),
    ],
    ids=[
        "unknown-dependency",
        "unknown-owner",
        "safe-outside-hard",
        "inverted",
        "missing-bound",
        "unprojected-kind",
    ],
)
def test_invalid_topology_is_rejected(mutate) -> None:
    data = copy.deepcopy(_raw())
    mutate(data)
    with pytest.raises(TopologyError):
        parse_topology(data)


def _obj(kind: str, name: str, **meta) -> dict:
    return {"kind": kind, "metadata": {"name": name, **meta}}


def _live_testbed() -> list[dict]:
    return [_obj(s.kind, s.name) for s in TOPO.resources.values()]


def test_ownership_resolves_complete_testbed() -> None:
    items = _live_testbed() + [
        _obj("ConfigMap", "kube-root-ca.crt"),  # created by Kubernetes in every namespace
        _obj("ConfigMap", "something-controller-made", ownerReferences=[{"kind": "X"}]),
    ]
    assert ownership_report(TOPO, items) == []


def test_ownership_flags_unmapped_object() -> None:
    items = _live_testbed() + [_obj("Deployment", "rogue")]
    assert ownership_report(TOPO, items) == [
        "Deployment/rogue is in the cluster but has no topology entry"
    ]


def test_ownership_flags_missing_object() -> None:
    items = [o for o in _live_testbed() if o["metadata"]["name"] != "payment-svc"]
    assert ownership_report(TOPO, items) == [
        "Service/payment-svc is declared but not in the cluster"
    ]


def test_ownership_flags_kind_mismatch() -> None:
    items = [o for o in _live_testbed() if o["metadata"]["name"] != "orders-config"]
    items.append(_obj("Service", "orders-config"))
    report = ownership_report(TOPO, items)
    assert "Service/orders-config is in the cluster but has no topology entry" in report
    assert "ConfigMap/orders-config is declared but not in the cluster" in report
