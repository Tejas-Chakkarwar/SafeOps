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
    assert keys["max_pool_size"] == {"hard_range": (1, 100), "safe_range": (5, 30)}
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
    ]
    assert ownership_report(TOPO, items) == []


def test_owner_referenced_objects_cannot_hide() -> None:
    """Milestone 2 review, B2: an owner reference must not exempt a projected-kind object."""
    items = _live_testbed() + [
        _obj("Service", "rogue-svc", ownerReferences=[{"kind": "Deployment", "name": "x"}]),
        _obj("Deployment", "rogue-dep", ownerReferences=[{"kind": "Whatever", "name": "y"}]),
    ]
    assert ownership_report(TOPO, items) == [
        "Service/rogue-svc is in the cluster but has no topology entry",
        "Deployment/rogue-dep is in the cluster but has no topology entry",
    ]


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


# --- the bound topology condition and the strict loader (decision 0025, M8A-2 topology fix) -----

import hashlib  # noqa: E402

from safeops.topology import (  # noqa: E402
    TOPOLOGY_PATH,
    TopologyCondition,
    load_topology_condition,
)

COMMITTED = TOPOLOGY_PATH.read_text()
EDGE = "  edge:      {depends_on: [gateway]}\n"  # a fifth caller: a content-distinct topology B


def _write(tmp_path, text: str | bytes):
    path = tmp_path / "topology.yaml"
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


def _with_edge() -> str:
    assert "  postgres:  {depends_on: [], stateful: true}\n" in COMMITTED
    return COMMITTED.replace(
        "  postgres:  {depends_on: [], stateful: true}\n",
        "  postgres:  {depends_on: [], stateful: true}\n" + EDGE,
    )


def test_a_condition_hashes_and_parses_the_same_bytes(tmp_path) -> None:
    raw = (COMMITTED + "# condition A\n").encode()
    condition = load_topology_condition(_write(tmp_path, raw))
    assert isinstance(condition, TopologyCondition)
    assert condition.sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert "edge" not in condition.topology.services
    assert condition.topology.dependents("orders") == {"gateway"}


def test_the_committed_condition_is_the_committed_files_bytes() -> None:
    condition = load_topology_condition()
    assert condition.sha256 == "sha256:" + hashlib.sha256(TOPOLOGY_PATH.read_bytes()).hexdigest()
    assert set(condition.topology.services) == {
        "gateway",
        "orders",
        "inventory",
        "payment",
        "postgres",
    }


def test_a_loaded_condition_does_not_follow_its_file(tmp_path) -> None:
    path = _write(tmp_path, COMMITTED)
    condition = load_topology_condition(path)
    hash_a = condition.sha256
    path.write_text(_with_edge())  # A -> B
    assert condition.sha256 == hash_a
    assert "edge" not in condition.topology.services
    assert condition.topology.dependents("gateway") == set()
    assert load_topology_condition(path).sha256 != hash_a  # B really is different
    with pytest.raises(AttributeError):
        condition.sha256 = "sha256:" + "0" * 64  # type: ignore[misc]


def test_load_topology_is_the_conditions_topology(tmp_path) -> None:
    path = _write(tmp_path, COMMITTED)
    assert (
        load_topology(path).resources.keys()
        == load_topology_condition(path).topology.resources.keys()
    )


def _edit(old: str, new: str) -> str:
    assert old in COMMITTED, old
    return COMMITTED.replace(old, new, 1)


STRICT_FILE_CASES = {
    "duplicate-key": (
        _edit(
            "  payment:   {depends_on: []}\n",
            "  payment:   {depends_on: []}\n  payment:   {depends_on: []}\n",
        ),
        "duplicate key",
    ),
    "merge-key": ("base: &b {services: {}}\n<<: *b\n", "merge keys"),
    "non-string-key": (COMMITTED + "1: x\n", "not a string"),
    "invalid-utf8": (COMMITTED.encode() + b"# \xff\n", "not UTF-8"),
    "invalid-yaml": ("services: [unclosed\n", "not valid YAML"),
    "top-level-list": ("- services\n- resources\n", "topology: expected a mapping"),
    "top-level-string": ('"not a topology mapping"\n', "topology: expected a mapping"),
    "top-level-number": ("7\n", "topology: expected a mapping"),
    "top-level-null": ("~\n", "topology: expected a mapping"),
    "stateful-quoted-false": (
        _edit("stateful: true", 'stateful: "false"'),
        "stateful: expected true or false",
    ),
    "stateful-zero": (_edit("stateful: true", "stateful: 0"), "stateful: expected true or false"),
    "stateful-one": (_edit("stateful: true", "stateful: 1"), "stateful: expected true or false"),
    "persistence-quoted-false": (
        _edit("persistence_affecting: true", 'persistence_affecting: "false"'),
        "persistence_affecting: expected true or false",
    ),
    "persistence-zero": (
        _edit("persistence_affecting: true", "persistence_affecting: 0"),
        "persistence_affecting: expected true or false",
    ),
    "persistence-one": (
        _edit("persistence_affecting: true", "persistence_affecting: 1"),
        "persistence_affecting: expected true or false",
    ),
    "hard-range-bool": (
        _edit("{hard_range: [0, 20],", "{hard_range: [true, 20],"),
        "finite numbers",
    ),
    "safe-range-bool": (_edit("safe_range: [2, 6]}", "safe_range: [false, 6]}"), "finite numbers"),
    "nan-endpoint": (_edit("{hard_range: [0, 20],", "{hard_range: [.nan, 20],"), "finite numbers"),
    "plus-infinity-endpoint": (
        _edit("{hard_range: [0, 20],", "{hard_range: [0, .inf],"),
        "finite numbers",
    ),
    "minus-infinity-endpoint": (
        _edit("{hard_range: [0, 20],", "{hard_range: [-.inf, 20],"),
        "finite numbers",
    ),
    "string-endpoint": (
        _edit("{hard_range: [0, 20],", '{hard_range: ["0", 20],'),
        "finite numbers",
    ),
    "null-resource-body": (
        _edit("  gateway-svc:\n    kind: Service\n    owner: gateway\n", "  gateway-svc:\n"),
        "resource gateway-svc: expected a mapping",
    ),
    "null-service-body": (
        _edit("  payment:   {depends_on: []}\n", "  payment:\n"),
        "service payment: expected a mapping",
    ),
    "string-service-body": (
        _edit("  payment:   {depends_on: []}\n", "  payment: idle\n"),
        "service payment: expected a mapping",
    ),
    "depends-on-string": (
        _edit("  gateway:   {depends_on: [orders]}\n", "  gateway:   {depends_on: orders}\n"),
        "depends_on must be a list",
    ),
    "services-list": (
        _edit("services:\n", "services:\n  - gateway\nunused:\n"),
        "services: expected a mapping",
    ),
    "approved-rule-string": (
        _edit(
            '      log_level: {enum: ["debug", "info", "warn", "error"]}', '      log_level: "bad"'
        ),
        "approved_keys.log_level: expected a mapping",
    ),
    "approved-rule-integer": (
        _edit(
            '      log_level: {enum: ["debug", "info", "warn", "error"]}', "      log_level: 123"
        ),
        "approved_keys.log_level: expected a mapping",
    ),
    "approved-rule-list": (
        _edit(
            '      log_level: {enum: ["debug", "info", "warn", "error"]}', "      log_level: [1, 2]"
        ),
        "approved_keys.log_level: expected a mapping",
    ),
    "approved-enum-string": (
        _edit('{enum: ["debug", "info", "warn", "error"]}', '{enum: "debug"}'),
        "enum: expected a non-empty list",
    ),
    "approved-keys-string": (
        _edit(
            "    approved_keys:\n      log_level",
            "    approved_keys: nope\n    unused:\n      log_level",
        ),
        "approved_keys: expected a mapping",
    ),
    "ranged-field-scalar": (
        _edit(
            "    replicas:   {hard_range: [0, 20],    safe_range: [2, 6]}\n", "    replicas: 5\n"
        ),
        "expected a mapping",
    ),
    "owner-list": (
        _edit(
            "    kind: Service\n    owner: gateway\n", "    kind: Service\n    owner: [gateway]\n"
        ),
        "is not a service",
    ),
}


@pytest.mark.parametrize(
    ("text", "match"), STRICT_FILE_CASES.values(), ids=STRICT_FILE_CASES.keys()
)
def test_a_malformed_topology_is_a_topology_error(tmp_path, text, match) -> None:
    with pytest.raises(TopologyError, match=match):
        load_topology_condition(_write(tmp_path, text))


def test_the_strict_loader_reports_topology_not_policy_errors(tmp_path) -> None:
    from safeops.policy import PolicyError

    with pytest.raises(TopologyError) as exc:
        load_topology_condition(_write(tmp_path, STRICT_FILE_CASES["duplicate-key"][0]))
    assert not isinstance(exc.value, PolicyError) and "topology.yaml" in str(exc.value)
    with pytest.raises(TopologyError, match="cannot be read"):
        load_topology_condition(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    "data", [["services"], "topology", 3, None, {"services": None, "resources": {"x": None}}]
)
def test_parse_topology_checks_shapes_before_reading_them(data) -> None:
    with pytest.raises(TopologyError):
        parse_topology(data)


def test_valid_shapes_still_load(tmp_path) -> None:
    text = _edit(
        "{hard_range: [0, 20],    safe_range: [2, 6]}",
        "{hard_range: [0.0, 20.5], safe_range: [2, 6.5]}",  # finite floats are ranges too
    )
    topo = load_topology_condition(_write(tmp_path, text)).topology
    assert topo.resources["gateway"].bounds["replicas"].hard.hi == 20.5
    assert topo.resources["db-pool-config"].persistence_affecting is True
    assert topo.stateful == frozenset({"postgres"})
    off = load_topology_condition(
        _write(tmp_path, _edit("persistence_affecting: true", "persistence_affecting: false"))
    ).topology
    assert off.resources["db-pool-config"].persistence_affecting is False
    assert off.resources["orders-config"].persistence_affecting is False  # absent: false
    assert dict(off.resources["orders-config"].approved_keys["log_level"]) == {
        "enum": ("debug", "info", "warn", "error")
    }
