"""Milestone 8A-2: the eight features over stored evidence (decision 0025 D§5, D§7.2, D§7.3).

Fabricated proposals and stored trajectory steps (tests/m8_fixtures.py), the real committed
topology and policy, and literal expected values (Rule 17).
"""

from __future__ import annotations

import dataclasses
import json
import math
import typing

import pytest

from safeops.domain import FeatureVector, TrajectoryStep
from safeops.features import conflict
from safeops.features.blast import ReplicaEvidence, TopologyEvidence
from safeops.features.evidence import latest
from safeops.features.extractor import RiskFeatureExtractor
from safeops.features.policy import ConflictRules, load_policy
from safeops.tools import contracts
from safeops.tools.canonical import result_digest
from tests.m8_fixtures import (
    DEPLOYMENT_BOUNDS,
    configmap,
    context,
    deployment,
    error,
    history,
    ok,
    patch_configmap,
    patch_service,
    pods_step,
    proposal,
    resources,
    restart,
    revision,
    rollback,
    scale,
    service,
    step,
    topology_with,
)

POLICY = load_policy()
EXTRACTOR = RiskFeatureExtractor(POLICY)
R, T = ReplicaEvidence, TopologyEvidence


def features(act, trajectory=(), ctx=None, extractor=EXTRACTOR, **kw) -> FeatureVector:
    return extractor.extract(proposal(act, **kw), list(trajectory), ctx or context())


def blast(act, trajectory=(), ctx=None):
    p = proposal(act)
    ev = EXTRACTOR.blast_evidence(p, list(trajectory), ctx or context())
    return features(act, trajectory, ctx).blast_radius, ev


def dep(name: str, replicas: int | None = 2, number: int = 1, **kw):
    return step(number, "get_deployment", name, ok(deployment(name, replicas, **kw)))


# --- fixtures are faithful stored evidence ----------------------------------------------------


@pytest.mark.parametrize(
    "s",
    [
        dep("orders"),
        dep("orders", None),
        dep("orders", 3, containers=("app", "sidecar"), count=2),
        step(1, "get_rollout_history", "orders", ok(history("orders", [revision(1, 2, 2)]))),
        step(
            1,
            "get_rollout_history",
            "orders",
            ok(history("orders", [revision(n, 0, 0) for n in range(3, 8)], total=7)),
        ),
        step(1, "get_configmap", "db-pool-config", ok(configmap("db-pool-config", {"a": "1"}))),
        step(1, "get_configmap", "x", ok(configmap("x", {"db_password": None, "k": "v"}))),
        step(1, "get_service", "orders-svc", ok(service("orders-svc", ready=0))),
        step(1, "get_service", "orders-svc", error()),
        pods_step(),
    ],
)
def test_fixture_steps_pass_the_real_contract_validator(s) -> None:
    assert contracts.step_problems(s.tool, s.args, s.result, s.result_digest) == []


def test_the_conflict_field_table_matches_the_frozen_contracts() -> None:
    """Hand-written in the feature layer (no safeops.tools import there), pinned here."""
    models = {tool: contracts.CONTRACTS[tool][1] for tool in conflict.FIELDS}
    for tool, fields in conflict.FIELDS.items():
        hints = typing.get_type_hints(models[tool], include_extras=False)
        for field, kind in fields.items():
            assert field in models[tool].model_fields, (tool, field)
            allowed = {a for a in typing.get_args(hints[field]) if a is not type(None)} or {
                hints[field]
            }
            base = {typing.get_origin(a) or a for a in allowed}
            assert any(kind is b or (kind is int and b is int) for b in base), (tool, field, base)


# --- irreversibility ---------------------------------------------------------------------------

IRREVERSIBILITY = {
    "rollback_deployment": (rollback(), 0.0),
    "restart_deployment": (restart("orders"), 0.2),
    "scale_deployment": (scale("orders", 3), 0.3),
    "patch_configmap": (patch_configmap("db-pool-config"), 0.5),
    "patch_service": (patch_service("orders-svc"), 0.4),
    "patch_deployment_resources": (resources("orders"), 0.6),
}


@pytest.mark.parametrize(("act", "value"), IRREVERSIBILITY.values(), ids=IRREVERSIBILITY.keys())
def test_irreversibility_is_one_minus_the_configured_reversibility(act, value) -> None:
    assert features(act).irreversibility == value  # float(1 - Fraction("0.8")) is exactly 0.2


# --- blast_radius: D§5.3's matrix --------------------------------------------------------------

BLAST = {
    "gateway-2": (restart("gateway"), [dep("gateway", 2)], 0.05, R.OBSERVED),
    "orders-2": (restart("orders"), [dep("orders", 2)], 0.30, R.OBSERVED),
    "inventory-2": (restart("inventory"), [dep("inventory", 2)], 0.55, R.OBSERVED),
    "inventory-20-observed": (restart("inventory"), [dep("inventory", 20)], 1.0, R.OBSERVED),
    "inventory-missing": (restart("inventory"), [], 1.0, R.NO_REPLICA_EVIDENCE),
    "gateway-missing": (restart("gateway"), [], 0.5, R.NO_REPLICA_EVIDENCE),
    "orders-missing": (restart("orders"), [], 0.75, R.NO_REPLICA_EVIDENCE),
    "scale-inventory-20-to-0": (scale("inventory", 0), [dep("inventory", 20)], 1.0, R.OBSERVED),
    "postgres-svc": (
        patch_service("postgres-svc", {"app": "postgres"}),
        [dep("orders", 2)],
        1.0,
        R.NO_DEPLOYMENT_WORKLOAD,
    ),
    "unresolvable": (
        restart("checkout"),
        [dep("checkout", 2)],
        1.0,
        R.UNRESOLVABLE_TARGET,
    ),
}


@pytest.mark.parametrize(("act", "traj", "value", "category"), BLAST.values(), ids=BLAST.keys())
def test_the_blast_radius_matrix(act, traj, value, category) -> None:
    got, ev = blast(act, traj)
    assert got == value
    assert ev.replica is category
    expected_topology = T.UNRESOLVABLE_TARGET if category is R.UNRESOLVABLE_TARGET else T.OBSERVED
    assert ev.topology is expected_topology


def test_observed_and_missing_inventory_share_1_0_but_not_their_evidence() -> None:
    observed, ev_observed = blast(restart("inventory"), [dep("inventory", 20)])
    missing, ev_missing = blast(restart("inventory"), [])
    assert observed == missing == 1.0
    assert (ev_observed.replica, ev_observed.replicas) == (R.OBSERVED, 20)
    assert (ev_missing.replica, ev_missing.replicas) == (R.NO_REPLICA_EVIDENCE, None)


def test_the_components_for_the_committed_topology() -> None:
    _, ev = blast(restart("orders"), [dep("orders", 2)])
    assert (ev.workload, ev.replicas, ev.dependents, ev.dependents_max) == ("orders", 2, 1, 2)
    _, ev = blast(restart("gateway"), [])
    assert (ev.dependents, ev.dependents_max) == (0, 2)


def test_scale_uses_the_larger_of_current_and_requested() -> None:
    assert blast(scale("gateway", 10), [dep("gateway", 2)])[0] == 0.25  # max(2, 10)/20, D 0
    assert blast(scale("gateway", 0), [dep("gateway", 6)])[0] == 0.15  # max(6, 0)/20
    assert blast(scale("gateway", 500), [dep("gateway", 2)])[0] == 0.5  # clamped for the feature


def test_scale_never_uses_the_requested_count_alone() -> None:
    got, ev = blast(scale("gateway", 2), [])  # current unknown
    assert (got, ev.replica) == (0.5, R.NO_REPLICA_EVIDENCE)


def test_missing_replica_evidence_keeps_the_known_topology_component() -> None:
    # orders: replica 1 (missing) + dependent 1/2: 0.75, not the whole-feature 1.0.
    assert blast(restart("orders"), [])[0] == 0.75


def test_the_dependent_component_counts_dependents_not_dependencies() -> None:
    # gateway calls orders (a dependency) but nothing calls gateway: D = 0.
    assert blast(restart("gateway"), [dep("gateway", 2)])[0] == 0.05


def test_d_max_comes_from_the_topology() -> None:
    # A fourth caller of orders (via a new service) raises D_max: the same action rescales.
    import yaml

    from safeops.topology import TOPOLOGY_PATH, parse_topology

    data = yaml.safe_load(TOPOLOGY_PATH.read_text())
    data["services"]["edge"] = {"depends_on": ["gateway"]}
    topo = parse_topology(data)
    # dependents(postgres) = {orders, gateway, edge}: D_max = 3; dependents(orders) = 2.
    got, ev = blast(restart("orders"), [dep("orders", 2)], context(topology=topo))
    assert (ev.dependents, ev.dependents_max) == (2, 3)
    assert got == pytest.approx((0.1 + 2 / 3) / 2, abs=1e-15)


def test_patch_configmap_and_patch_service_use_the_owners_deployment() -> None:
    got, ev = blast(patch_configmap("db-pool-config"), [dep("orders", 4)])
    assert (got, ev.workload, ev.replica) == (0.35, "orders", R.OBSERVED)  # (0.2 + 0.5)/2
    got, ev = blast(patch_service("inventory-svc", {"app": "inventory"}), [dep("inventory", 2)])
    assert (got, ev.workload, ev.replica) == (0.55, "inventory", R.OBSERVED)


@pytest.mark.parametrize("owned", [0, 2], ids=["zero-owned", "two-owned"])
def test_a_non_unique_owned_deployment_is_classified_not_raised(owned) -> None:
    extra = {
        f"orders-{i}": {"kind": "Deployment", "owner": "payment", **DEPLOYMENT_BOUNDS}
        for i in range(owned - 1)
    }
    extra["payment-config"] = {
        "kind": "ConfigMap",
        "owner": "payment",
        "approved_keys": {"retries": {"hard_range": [0, 10], "safe_range": [1, 5]}},
    }
    if owned == 0:  # payment's one Deployment re-owned elsewhere: payment owns none
        extra["payment"] = {"kind": "Deployment", "owner": "inventory", **DEPLOYMENT_BOUNDS}
    topo = topology_with(extra)
    act = patch_configmap("payment-config", {"retries": 3})
    trajectory = [dep("payment", 2), dep("orders-0", 2, number=2)][: 1 + (owned > 1)]
    try:
        fv = features(act, trajectory, context(topology=topo))
    except Exception as exc:  # the extractor must classify, never raise (D§5.3)
        pytest.fail(f"extraction raised for a non-unique workload: {exc!r}")
    ev = EXTRACTOR.blast_evidence(proposal(act), trajectory, context(topology=topo))
    assert (ev.replica, ev.workload) == (R.NO_DEPLOYMENT_WORKLOAD, None)
    assert fv.blast_radius == 1.0  # replica 1, payment's dependents {orders, gateway}: 2/2


def test_an_unresolvable_target_fails_closed_without_topology_error() -> None:
    cases = [
        restart("checkout"),  # not in the ownership map
        restart("orders-svc"),  # a Service named where the action needs a Deployment
        patch_configmap("orders-config", {"log_level": "info"}).model_copy(
            update={"target": rollback().target.model_copy(update={"kind": "Deployment"})}
        ),
        restart("orders").model_copy(
            update={"target": restart("orders").target.model_copy(update={"ns": "kube-system"})}
        ),
    ]
    for act in cases:
        fv = features(act, [dep(act.target.name, 2)])
        assert (fv.blast_radius, fv.scope_outside_target) == (1.0, 1.0), act


# --- persistence_risk ------------------------------------------------------------------------


def test_persistence_risk_is_narrow() -> None:
    assert features(patch_configmap("db-pool-config")).persistence_risk == 1.0
    assert features(patch_configmap("orders-config", {"log_level": "info"})).persistence_risk == 0.0
    assert features(patch_service("postgres-svc", {"app": "postgres"})).persistence_risk == 0.0
    assert features(patch_configmap("checkout-config")).persistence_risk == 1.0  # unresolvable
    assert features(restart("checkout")).persistence_risk == 0.0  # unresolvable, not a ConfigMap
    for act, _ in IRREVERSIBILITY.values():
        if act.type.value != "patch_configmap":
            assert features(act).persistence_risk == 0.0


# --- scope_outside_target -----------------------------------------------------------------------


def test_scope_is_the_harm_predicate_direction() -> None:
    orders = context("orders")
    assert features(restart("inventory"), ctx=orders).scope_outside_target == 0.0  # a dependency
    assert features(restart("gateway"), ctx=orders).scope_outside_target == 1.0  # a dependent
    assert features(restart("orders"), ctx=orders).scope_outside_target == 0.0
    assert features(patch_service("postgres-svc"), ctx=orders).scope_outside_target == 0.0
    assert features(restart("checkout"), ctx=orders).scope_outside_target == 1.0
    assert features(restart("orders"), ctx=context("inventory")).scope_outside_target == 1.0


# --- confidence_deficit -------------------------------------------------------------------------


@pytest.mark.parametrize(("confidence", "deficit"), [(0.0, 1.0), (1.0, 0.0), (0.8, 0.2)])
def test_confidence_deficit(confidence, deficit) -> None:
    got = features(restart("orders"), confidence=confidence).confidence_deficit
    assert math.isclose(got, deficit, rel_tol=0.0, abs_tol=1e-12)
    assert got == 1.0 - confidence  # float arithmetic on the stored float


# --- evidence_insufficiency: corroboration, not coverage ------------------------------------------

HEALTHY_1 = step(
    1,
    "get_rollout_history",
    "orders",
    ok(history("orders", [revision(1, 2, 2), revision(2, 2, 0)])),
)


def _ins(act, trajectory) -> float:
    return features(act, trajectory).evidence_insufficiency


def test_rollback_evidence_counts_semantic_support() -> None:
    assert _ins(rollback(to=1), []) == 1.0
    assert _ins(rollback(to=1), [pods_step()]) == 1.0  # a successful call alone earns nothing
    unhealthy = step(1, "get_rollout_history", "orders", ok(history("orders", [revision(1, 2, 1)])))
    assert _ins(rollback(to=1), [unhealthy]) == pytest.approx(2 / 3, abs=0)  # retrieved only
    assert _ins(rollback(to=1), [HEALTHY_1]) == pytest.approx(1 / 3, abs=0)  # 2 of 3


def test_rollback_never_goes_below_its_pre_v1_floor() -> None:
    assert _ins(rollback(to=1), [HEALTHY_1, dep("orders", 2, number=2)]) == 1 / 3
    assert _ins(rollback(to=1), [HEALTHY_1]) > 0


@pytest.mark.parametrize(
    ("revisions", "to", "healthy"),
    [
        ([revision(1, 2, 2)], 1, True),
        ([revision(1, 2, 1)], 1, False),  # ready_replicas > 0 is not enough
        ([revision(1, 0, 0)], 1, False),  # scaled down after a completed rollout
        ([revision(2, 2, 2)], 1, False),  # the proposed revision is absent
        ([revision(n, 0, 0) for n in range(3, 8)], 1, False),  # absent from a truncated list
    ],
    ids=["healthy", "partly-ready", "scaled-to-zero", "absent", "absent-truncated"],
)
def test_revision_health_is_replicas_positive_and_all_ready(revisions, to, healthy) -> None:
    total = 7 if len(revisions) == 5 else None
    s = step(1, "get_rollout_history", "orders", ok(history("orders", revisions, total)))
    got = _ins(rollback(to=to), [s])
    assert got == (pytest.approx(1 / 3, abs=0) if healthy else pytest.approx(2 / 3, abs=0))


def test_history_with_no_revisions_is_not_retrieved() -> None:
    empty = step(1, "get_rollout_history", "orders", ok(history("orders", [])))
    assert _ins(rollback(to=1), [empty]) == 1.0


SINGLE_REQUIREMENTS = {
    "restart-found": (restart("orders"), [dep("orders", 2)], 0.0),
    "restart-not-found": (restart("orders"), [dep("orders", None)], 1.0),
    "restart-error": (restart("orders"), [step(1, "get_deployment", "orders", error())], 1.0),
    "restart-other-deployment": (restart("orders"), [dep("inventory", 2)], 1.0),
    "scale-observed": (scale("orders", 3), [dep("orders", 2)], 0.0),
    "scale-zero-replicas": (scale("orders", 3), [dep("orders", 0)], 0.0),
    "scale-not-found": (scale("orders", 3), [dep("orders", None)], 1.0),
    "resources-container": (resources("orders", "app"), [dep("orders", 2)], 0.0),
    "resources-other-container": (resources("orders", "sidecar"), [dep("orders", 2)], 1.0),
    "resources-hidden-by-bound": (
        resources("orders", "sidecar"),
        [dep("orders", 2, containers=("app",), count=11)],
        1.0,
    ),
    "configmap-key": (
        patch_configmap("db-pool-config"),
        [
            step(
                1,
                "get_configmap",
                "db-pool-config",
                ok(configmap("db-pool-config", {"max_pool_size": "10"})),
            )
        ],
        0.0,
    ),
    "configmap-one-key-missing": (
        patch_configmap("db-pool-config", {"max_pool_size": 20, "connection_timeout_ms": 900}),
        [
            step(
                1,
                "get_configmap",
                "db-pool-config",
                ok(configmap("db-pool-config", {"max_pool_size": "10"})),
            )
        ],
        1.0,
    ),
    "configmap-truncated-missing": (
        patch_configmap("db-pool-config"),
        [
            step(
                1,
                "get_configmap",
                "db-pool-config",
                ok(configmap("db-pool-config", {f"k{i:02}": "v" for i in range(64)}, total=65)),
            )
        ],
        1.0,
    ),
    "configmap-redacted": (
        patch_configmap("db-pool-config", {"db_password": "x"}),
        [
            step(
                1,
                "get_configmap",
                "db-pool-config",
                ok(configmap("db-pool-config", {"db_password": None})),
            )
        ],
        1.0,
    ),
    "service-observed": (
        patch_service("orders-svc"),
        [step(1, "get_service", "orders-svc", ok(service("orders-svc", ready=0)))],
        0.0,
    ),
    "service-error": (
        patch_service("orders-svc"),
        [step(1, "get_service", "orders-svc", error("timeout"))],
        1.0,
    ),
}


@pytest.mark.parametrize(
    ("act", "trajectory", "value"), SINGLE_REQUIREMENTS.values(), ids=SINGLE_REQUIREMENTS.keys()
)
def test_single_requirement_predicates(act, trajectory, value) -> None:
    assert _ins(act, trajectory) == value


# --- malformed stored evidence establishes nothing, and never raises ------------------------------


def _raw(tool: str, name: str, result, args=None):
    """A step built without the contract's help: arbitrary stored JSON."""
    return step(1, tool, name, result, args=args)


MALFORMED = [
    _raw("get_deployment", "orders", {"status": "ok"}),
    _raw("get_deployment", "orders", {"status": "ok", "data": []}),
    _raw("get_deployment", "orders", {"status": "ok", "data": {"found": "yes", "replicas": 2}}),
    _raw("get_deployment", "orders", {"status": "ok", "data": {"found": True, "replicas": True}}),
    _raw("get_deployment", "orders", {"status": "ok", "data": {"found": True, "replicas": -1}}),
    _raw("get_deployment", "orders", {"status": "ok", "data": {"found": True, "replicas": 2.0}}),
    _raw("get_deployment", "orders", {"status": "ok", "data": {"found": True, "containers": [7]}}),
    _raw("get_deployment", "orders", ["not", "an", "envelope"]),
    _raw("get_deployment", "orders", None),
    _raw("get_deployment", "orders", {"status": "OK", "data": deployment("orders")}),
    _raw("get_deployment", "orders", ok(deployment("orders")), args={"namespace": "testbed"}),
    _raw("get_deployment", "orders", ok(deployment("orders")), args={"name": "orders"}),
    _raw(
        "get_deployment",
        "orders",
        ok(deployment("orders")),
        args={"namespace": "testbed", "name": "orders", "extra": 1},
    ),
    _raw(
        "get_deployment",
        "orders",
        ok(deployment("orders")),
        args={"namespace": 7, "name": "orders"},
    ),
    _raw("get_rollout_history", "orders", {"status": "ok", "data": {"revisions": {"1": {}}}}),
    _raw(
        "get_rollout_history",
        "orders",
        {
            "status": "ok",
            "data": {"revision_count": 1, "revisions": [None, "x", {"revision": "1"}]},
        },
    ),
    _raw(
        "get_rollout_history",
        "orders",
        {
            "status": "ok",
            "data": {
                "revision_count": True,
                "revisions": [{"revision": 1, "replicas": True, "ready_replicas": True}],
            },
        },
    ),
    _raw(
        "get_configmap", "db-pool-config", {"status": "ok", "data": {"found": True, "entries": "x"}}
    ),
    _raw(
        "get_configmap",
        "db-pool-config",
        {
            "status": "ok",
            "data": {
                "found": True,
                "entries": [{"key": "max_pool_size", "redacted": False, "value": 20}],
            },
        },
    ),
    _raw(
        "get_service",
        "orders-svc",
        {"status": "ok", "data": {"found": True, "selector": [], "ready_endpoint_count": 1}},
    ),
]
MALFORMED_ACTIONS = [
    restart("orders"),
    scale("orders", 3),
    resources("orders", "app"),
    rollback(to=1),
    patch_configmap("db-pool-config"),
    patch_service("orders-svc"),
]


@pytest.mark.parametrize("bad", MALFORMED, ids=range(len(MALFORMED)))
@pytest.mark.parametrize("act", MALFORMED_ACTIONS, ids=lambda a: a.type.value)
def test_malformed_stored_evidence_never_raises_and_lowers_no_risk(bad, act) -> None:
    fv = features(act, [bad], claims=["prior_revision_healthy"])
    assert fv.prerequisites_unverified == 1.0
    _, ev = blast(act, [bad])
    assert ev.replica is not R.OBSERVED
    for name in FeatureVector.model_fields:
        assert 0.0 <= getattr(fv, name) <= 1.0


# Malformed in exactly the fields the action's own predicate reads: it is not established.
PREDICATE_MALFORMED = {
    "restart-found-not-true": (restart("orders"), MALFORMED[2]),
    "restart-no-data": (restart("orders"), MALFORMED[0]),
    "restart-data-not-object": (restart("orders"), MALFORMED[1]),
    "restart-wrong-status": (restart("orders"), MALFORMED[9]),
    "restart-args-missing-name": (restart("orders"), MALFORMED[10]),
    "restart-args-extra": (restart("orders"), MALFORMED[12]),
    "restart-args-wrong-type": (restart("orders"), MALFORMED[13]),
    "scale-replicas-bool": (scale("orders", 3), MALFORMED[3]),
    "scale-replicas-negative": (scale("orders", 3), MALFORMED[4]),
    "scale-replicas-float": (scale("orders", 3), MALFORMED[5]),
    "resources-container-not-object": (resources("orders"), MALFORMED[6]),
    "rollback-revisions-not-list": (rollback(to=1), MALFORMED[14]),
    "rollback-count-bool-entries-bool": (rollback(to=1), MALFORMED[16]),
    "configmap-entries-not-list": (patch_configmap("db-pool-config"), MALFORMED[17]),
    "configmap-value-not-string": (patch_configmap("db-pool-config"), MALFORMED[18]),
    "service-selector-not-object": (patch_service("orders-svc"), MALFORMED[19]),
}


@pytest.mark.parametrize(
    ("act", "bad"), PREDICATE_MALFORMED.values(), ids=PREDICATE_MALFORMED.keys()
)
def test_malformed_predicate_fields_establish_nothing(act, bad) -> None:
    assert features(act, [bad]).evidence_insufficiency == 1.0


def test_well_formed_fields_still_count_beside_malformed_ones() -> None:
    """Not a blanket refusal: a predicate reads only its own fields (D§7.3)."""
    assert features(restart("orders"), [MALFORMED[3]]).evidence_insufficiency == 0.0
    assert features(rollback(to=1), [MALFORMED[15]]).evidence_insufficiency == 2 / 3


def test_the_latest_observation_is_the_greatest_step_not_list_position() -> None:
    first, later = dep("orders", 2, number=1), dep("orders", 7, number=4)
    for trajectory in ([first, later], [later, first]):
        assert latest(trajectory, "get_deployment", "testbed", "orders")["replicas"] == 7
        assert blast(restart("orders"), trajectory)[1].replicas == 7


def test_a_later_failed_read_does_not_hide_an_earlier_success() -> None:
    trajectory = [dep("orders", 2, number=1), step(2, "get_deployment", "orders", error())]
    assert blast(restart("orders"), trajectory)[1].replicas == 2


def test_a_later_not_found_overrides_an_earlier_count() -> None:
    trajectory = [dep("orders", 2, number=1), dep("orders", None, number=2)]
    assert blast(restart("orders"), trajectory)[1].replica is R.NO_REPLICA_EVIDENCE


# --- prerequisites_unverified ---------------------------------------------------------------------


def test_prerequisites() -> None:
    unhealthy = step(1, "get_rollout_history", "orders", ok(history("orders", [revision(1, 2, 1)])))

    def pu(act, trajectory, claims):
        return features(act, trajectory, claims=claims).prerequisites_unverified

    assert pu(rollback(to=1), [], []) == 0.0  # no claims
    assert pu(rollback(to=1), [HEALTHY_1], ["prior_revision_healthy"]) == 0.0
    assert pu(rollback(to=1), [unhealthy], ["prior_revision_healthy"]) == 1.0
    assert pu(rollback(to=1), [], ["prior_revision_healthy"]) == 1.0  # never queried
    assert pu(rollback(to=1), [HEALTHY_1], ["image_was_pulled"]) == 1.0  # unknown id
    assert pu(rollback(to=1), [HEALTHY_1], ["prior_revision_healthy", "image_was_pulled"]) == 0.5
    assert pu(rollback(to=2), [HEALTHY_1], ["prior_revision_healthy"]) == 1.0  # rev 2 not ready
    assert pu(restart("orders"), [HEALTHY_1], ["prior_revision_healthy"]) == 1.0  # not a rollback


def test_a_prerequisite_outside_the_loaded_vocabulary_is_unsupported() -> None:
    shrunk = dataclasses.replace(
        POLICY, evidence=dataclasses.replace(POLICY.evidence, prerequisites=frozenset())
    )
    fv = features(
        rollback(to=1),
        [HEALTHY_1],
        extractor=RiskFeatureExtractor(shrunk),
        claims=["prior_revision_healthy"],
    )
    assert fv.prerequisites_unverified == 1.0


# --- telemetry_conflict: the seam, with a test-only rule set --------------------------------


def _with_rules(*rules: conflict.ConflictRule) -> RiskFeatureExtractor:
    return RiskFeatureExtractor(
        dataclasses.replace(POLICY, conflict=ConflictRules(rules, POLICY.conflict.sha256))
    )


NO_ENDPOINTS = conflict.ConflictRule("no-endpoints", "get_service", "ready_endpoint_count", "eq", 0)
MANY_REPLICAS = conflict.ConflictRule("many", "get_deployment", "replicas", "gt", 10)


def test_production_rules_give_zero_through_the_evaluator() -> None:
    svc = [step(1, "get_service", "orders-svc", ok(service("orders-svc", ready=0)))]
    assert features(patch_service("orders-svc"), svc).telemetry_conflict == 0.0
    assert (
        _with_rules(NO_ENDPOINTS)
        .extract(proposal(patch_service("orders-svc")), svc, context())
        .telemetry_conflict
        == 1.0
    )  # the same evidence, a rule configured: it fires


@pytest.mark.parametrize(
    ("trajectory", "value"),
    [
        ([], 0.0),  # no matching observation
        ([step(1, "get_service", "orders-svc", error())], 0.0),
        ([step(1, "get_service", "inventory-svc", ok(service("inventory-svc", ready=0)))], 0.0),
        ([step(1, "get_service", "orders-svc", ok(service("orders-svc", ready=2)))], 0.0),
        ([step(1, "get_service", "orders-svc", ok(service("orders-svc", ready=0)))], 1.0),
        (
            [
                step(1, "get_service", "orders-svc", ok(service("orders-svc", ready=0))),
                step(2, "get_service", "orders-svc", ok(service("orders-svc", ready=2))),
            ],
            0.0,  # the latest observation decides
        ),
        (
            [
                step(
                    1,
                    "get_service",
                    "orders-svc",
                    {"status": "ok", "data": {"ready_endpoint_count": None}},
                )
            ],
            0.0,  # a null never satisfies a comparison
        ),
        (
            [
                step(
                    1,
                    "get_service",
                    "orders-svc",
                    {"status": "ok", "data": {"ready_endpoint_count": False}},
                )
            ],
            0.0,  # a bool is not an integer
        ),
    ],
    ids=["none", "error", "other-target", "unsatisfied", "satisfied", "latest", "null", "bool"],
)
def test_one_test_rule(trajectory, value) -> None:
    fv = _with_rules(NO_ENDPOINTS).extract(
        proposal(patch_service("orders-svc")), trajectory, context()
    )
    assert fv.telemetry_conflict == value


def test_any_rule_firing_is_a_conflict() -> None:
    extractor = _with_rules(MANY_REPLICAS, NO_ENDPOINTS)
    svc = [step(1, "get_service", "orders-svc", ok(service("orders-svc", ready=0)))]
    assert (
        extractor.extract(proposal(patch_service("orders-svc")), svc, context()).telemetry_conflict
        == 1.0
    )
    svc_ok = [step(1, "get_service", "orders-svc", ok(service("orders-svc", ready=2)))]
    assert (
        extractor.extract(
            proposal(patch_service("orders-svc")), svc_ok, context()
        ).telemetry_conflict
        == 0.0
    )


@pytest.mark.parametrize(
    ("op", "value", "fires"),
    [
        ("eq", 12, True),
        ("ne", 12, False),
        ("lt", 13, True),
        ("le", 12, True),
        ("gt", 12, False),
        ("ge", 12, True),
    ],
)
def test_each_operator(op, value, fires) -> None:
    rule = conflict.ConflictRule("r", "get_deployment", "replicas", op, value)
    fv = _with_rules(rule).extract(proposal(restart("orders")), [dep("orders", 12)], context())
    assert fv.telemetry_conflict == (1.0 if fires else 0.0)


# --- totality and the input contract --------------------------------------------------------------


def test_a_fully_observed_restart_is_low_risk_and_exact() -> None:
    fv = features(restart("orders"), [dep("orders", 2)], confidence=0.75)
    assert fv == FeatureVector(
        irreversibility=0.2,
        blast_radius=0.3,
        persistence_risk=0.0,
        scope_outside_target=0.0,
        confidence_deficit=0.25,
        evidence_insufficiency=0.0,
        prerequisites_unverified=0.0,
        telemetry_conflict=0.0,
    )


def test_a_catastrophic_proposal_scores_the_maximum_where_it_should() -> None:
    act = patch_configmap("checkout-config")  # unresolvable ConfigMap patch, nothing observed
    fv = features(act, [], confidence=0.0, claims=["prior_revision_healthy"])
    assert fv == FeatureVector(
        irreversibility=0.5,
        blast_radius=1.0,
        persistence_risk=1.0,
        scope_outside_target=1.0,
        confidence_deficit=1.0,
        evidence_insufficiency=1.0,
        prerequisites_unverified=1.0,
        telemetry_conflict=0.0,
    )


def test_the_extractor_takes_the_domain_proposal_only() -> None:
    from safeops.agent.parser import ParsedProposal
    from safeops.enums import ProposalStatus

    p = proposal(restart("orders"))
    with pytest.raises(TypeError, match="domain Proposal"):
        EXTRACTOR.extract(ParsedProposal(ProposalStatus.VALID_ACTION, p), [], context())
    with pytest.raises(ValueError, match="no action"):
        EXTRACTOR.extract(p.model_copy(update={"recommended_action": None}), [], context())
    with pytest.raises(TypeError, match="TrajectoryStep"):
        EXTRACTOR.extract(p, [{"step": 1}], context())
    with pytest.raises(TypeError, match="IncidentRuntimeContext"):
        EXTRACTOR.extract(p, [], object())
    with pytest.raises(ValueError, match="structurally valid"):
        EXTRACTOR.extract(
            proposal(scale("orders", 3).model_copy(update={"params": {"replicas": "3"}})),
            [],
            context(),
        )
    with pytest.raises(TypeError, match="PolicyBundle"):
        RiskFeatureExtractor(None)


def test_extraction_never_reads_now() -> None:
    a = features(restart("orders"), [dep("orders", 2)])
    ctx = context()
    later = type(ctx)(alert=ctx.alert, topology=ctx.topology, now=ctx.now.replace(year=2030))
    assert features(restart("orders"), [dep("orders", 2)], later) == a


# --- extraction reads the stored form (review fix) ------------------------------------------------


def _live(data: dict) -> TrajectoryStep:
    """A live step whose result isn't plain JSON; its digest is that of its stored form."""
    stored = json.loads(json.dumps({"status": "ok", "data": data}, default=list))
    return TrajectoryStep(
        step=1,
        tool="get_rollout_history",
        args={"namespace": "testbed", "name": "orders"},
        result={"status": "ok", "data": data},
        result_digest=result_digest(stored),
        tokens_in=None,
        tokens_out=None,
        latency_ms=None,
    )


def test_a_live_tuple_is_read_as_the_list_the_record_stores() -> None:
    plain = history("orders", [revision(1, 2, 2)])
    live = _live({**plain, "revisions": tuple(plain["revisions"])})
    stored = [TrajectoryStep.model_validate_json(live.model_dump_json())]
    p = proposal(rollback(to=1), claims=["prior_revision_healthy"])
    at_run_time = EXTRACTOR.extract(p, [live], context())
    assert at_run_time == EXTRACTOR.extract(p, stored, context())
    assert (at_run_time.evidence_insufficiency, at_run_time.prerequisites_unverified) == (
        1 / 3,
        0.0,
    )


def test_a_step_with_no_stored_form_is_a_harness_error() -> None:
    from types import MappingProxyType

    bad = TrajectoryStep(
        step=1,
        tool="get_deployment",
        args={"namespace": "testbed", "name": "orders"},
        result={"status": "ok", "data": MappingProxyType(deployment("orders", 2))},
        result_digest="sha256:" + "0" * 64,
        tokens_in=None,
        tokens_out=None,
        latency_ms=None,
    )
    with pytest.raises(ValueError, match="no strict-JSON stored form"):
        features(restart("orders"), [bad])
