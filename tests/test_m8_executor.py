"""Milestone 8B-1: the offline executor (decision 0025 D§10-D§13, M8B-1 amendment).

The real ActionExecutor over offline transports (tests/m8_executor_fakes.py): structural
re-validation, the hard envelope, the six exact requests, preflight-before-write, the
semantic write-reply classes, bounded reconciliation, exact quantities, and the producer
shapes of all five ExecutionStatus values. No cluster, no LLM.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import timedelta

import pytest

from safeops.domain import MAX_DISCARD_ERROR, ActionTarget, RecommendedAction
from safeops.enums import ActionType, ExecutionStatus
from safeops.executor import quantity
from safeops.executor.executor import (
    READ_ATTEMPTS,
    READ_RETRY_SLEEP_S,
    READ_TIMEOUT_S,
    RESTART_ANNOTATION,
    ActionExecutor,
    ExecutorConfigurationError,
    render_configmap_value,
)
from safeops.executor.result import ExecutionResult, ExecutionStepResult
from safeops.executor.transport import PatchType, ReadOutcome, ReadReply, WriteClass, WriteReply
from safeops.topology import parse_topology
from tests.m8_executor_fakes import (
    NS,
    SEMANTICS,
    T0,
    Clock,
    FakeCluster,
    Rig,
    action,
    patch_configmap,
    patch_service,
    resources,
    restart,
    rollback,
    scale,
    seed_objects,
)
from tests.m8_fixtures import TOPOLOGY

S = ExecutionStatus


def _shape(result: ExecutionResult) -> tuple:
    return (result.status, [(s.operation.value, s.status) for s in result.steps])


def _topology_with(**resources_override):
    """The committed topology with resources added or replaced."""
    import yaml

    from safeops.topology import TOPOLOGY_PATH

    raw = yaml.safe_load(TOPOLOGY_PATH.read_text())
    raw["resources"].update(resources_override)
    return parse_topology(raw)


# --- 1. structure is re-validated first --------------------------------------------------------

MALFORMED = {
    "scale-missing-replicas": ("scale_deployment", "Deployment", "orders", {}),
    "scale-replicas-a-string": ("scale_deployment", "Deployment", "orders", {"replicas": "15"}),
    "scale-replicas-a-bool": ("scale_deployment", "Deployment", "orders", {"replicas": True}),
    "scale-replicas-a-float": ("scale_deployment", "Deployment", "orders", {"replicas": 15.0}),
    "restart-unexpected-params": ("restart_deployment", "Deployment", "orders", {"now": 1}),
    "configmap-empty": ("patch_configmap", "ConfigMap", "db-pool-config", {}),
    "configmap-list-value": ("patch_configmap", "ConfigMap", "db-pool-config", {"k": [1]}),
    "configmap-bool-value": ("patch_configmap", "ConfigMap", "db-pool-config", {"k": True}),
    "resources-string-quantity": (
        "patch_deployment_resources",
        "Deployment",
        "orders",
        {"container": "orders", "requests": {"cpu_millis": "100"}},
    ),
    "resources-unknown-field": (
        "patch_deployment_resources",
        "Deployment",
        "orders",
        {"container": "orders", "limits": {"gpu": 1}},
    ),
    "resources-no-section": (
        "patch_deployment_resources",
        "Deployment",
        "orders",
        {"container": "orders"},
    ),
    "rollback-revision-zero": ("rollback_deployment", "Deployment", "orders", {"to_revision": 0}),
    "service-bad-label": ("patch_service", "Service", "orders-svc", {"selector": {"app": "a b"}}),
    "target-kind-lowercase": ("restart_deployment", "deployment", "orders", {}),
    "target-name-traversal": ("restart_deployment", "Deployment", "../orders", {}),
}


@pytest.mark.parametrize(
    ("type_", "kind", "name", "params"), MALFORMED.values(), ids=MALFORMED.keys()
)
def test_a_structurally_invalid_action_is_a_harness_error_before_anything(
    type_, kind, name, params
):
    rig = Rig(unavailable=99)  # a reader that would fail: never reached
    with pytest.raises(ValueError, match="structurally valid action"):
        rig.apply(action(type_, kind, name, params))
    assert rig.untouched


@pytest.mark.parametrize(
    "malformed",
    [
        # Structurally broken AND outside E1 / E2 / E3: the structural error wins.
        action("scale_deployment", "StatefulSet", "postgres", {"replicas": "1"}),
        action("restart_deployment", "Deployment", "orders", {"x": 1}, ns="kube-system"),
        action("scale_deployment", "Deployment", "checkout", {}),
        action(
            "patch_deployment_resources",
            "Deployment",
            "orders",
            {"container": "Bad_Name", "limits": {"cpu_millis": 99999}},
        ),
    ],
    ids=["e1", "e2", "e3", "hard-range"],
)
def test_structure_is_checked_before_the_envelope(malformed) -> None:
    rig = Rig()
    with pytest.raises(ValueError, match="structurally valid action"):
        rig.apply(malformed)
    assert rig.untouched


def test_apply_takes_a_recommended_action() -> None:
    rig = Rig()
    with pytest.raises(TypeError):
        rig.apply(restart().model_dump())
    assert rig.untouched


# --- 2. the common envelope: E1 -> E2 -> E3, the first failure wins --------------------------

COMMON = {
    "e1-statefulset": (restart("postgres", kind="StatefulSet"), "E1"),
    "e1-service-for-a-deployment-action": (restart("orders-svc", kind="Service"), "E1"),
    "e1-configmap-kind-for-scale": (
        action("scale_deployment", "ConfigMap", "orders-config", {"replicas": 1}),
        "E1",
    ),
    "e2-other-namespace": (restart(ns="kube-system"), "E2"),
    "e3-unknown-deployment": (restart("checkout"), "E3"),
    "e3-unknown-configmap": (patch_configmap({"x": 1}, name="kube-root-ca.crt"), "E3"),
    # every check fails: E1's reason is the one recorded
    "e1-first": (restart("checkout", kind="StatefulSet", ns="kube-system"), "E1"),
    "e2-before-e3": (restart("checkout", ns="kube-system"), "E2"),
}


@pytest.mark.parametrize(("act", "check"), COMMON.values(), ids=COMMON.keys())
def test_the_common_envelope_refuses_with_its_first_failure(act, check) -> None:
    rig = Rig(unavailable=99)
    result = rig.apply(act)
    assert result.status is S.REFUSED_BY_ENVELOPE and result.steps == ()
    assert result.error.startswith(f"{check}:")
    assert rig.untouched


def test_a_configmap_without_approved_keys_is_not_whitelisted() -> None:
    topology = _topology_with(**{"bare-config": {"kind": "ConfigMap", "owner": "orders"}})
    rig = Rig(topology=topology)
    result = rig.apply(patch_configmap({"x": "1"}, name="bare-config"))
    assert _shape(result) == (S.REFUSED_BY_ENVELOPE, [])
    assert result.error == "E3: ConfigMap/bare-config has no approved keys"
    assert rig.untouched


def test_the_namespace_is_the_configured_one_not_a_literal() -> None:
    rig = Rig(namespace="other-ns")
    assert rig.apply(restart()).error.startswith("E2:")
    assert rig.apply(restart(ns="other-ns")).status is S.APPLIED
    assert rig.writer.writes[0].namespace == "other-ns"


# --- 3. per-action hard envelope (never a safe range) -------------------------------------------


@pytest.mark.parametrize("replicas", [-1, 21, 500])
def test_scale_outside_the_hard_range_is_refused(replicas) -> None:
    rig = Rig()
    result = rig.apply(scale(replicas))
    assert _shape(result) == (S.REFUSED_BY_ENVELOPE, [])
    assert "hard range" in result.error and rig.untouched


@pytest.mark.parametrize("replicas", [0, 1, 15, 20])  # 0, 1 and 15 are outside safe 2..6
def test_scale_inside_the_hard_range_executes(replicas) -> None:
    rig = Rig()
    assert _shape(rig.apply(scale(replicas))) == (S.APPLIED, [("scale_deployment", "applied")])
    assert rig.cluster.get("Deployment", "orders")["spec"]["replicas"] == replicas


CONFIGMAP_REFUSED = {
    "unknown-key": ({"bogus": 1}, "not an approved key"),
    "numeric-string": ({"max_pool_size": "20"}, "not a JSON number"),
    "above-hard": ({"max_pool_size": 5000}, "hard range"),
    "below-hard": ({"max_pool_size": 0}, "hard range"),
    "float-above-hard": ({"connection_timeout_ms": 30000.5}, "hard range"),
    "one-bad-key-refuses-all": ({"max_pool_size": 20, "bogus": 1}, "not an approved key"),
}


@pytest.mark.parametrize(
    ("values", "why"), CONFIGMAP_REFUSED.values(), ids=CONFIGMAP_REFUSED.keys()
)
def test_configmap_envelope_refusals(values, why) -> None:
    rig = Rig()
    result = rig.apply(patch_configmap(values))
    assert _shape(result) == (S.REFUSED_BY_ENVELOPE, []) and why in result.error
    assert rig.untouched
    assert rig.cluster.get("ConfigMap", "db-pool-config")["data"]["max_pool_size"] == "10"


@pytest.mark.parametrize("value", ["verbose", "INFO", 1])
def test_an_enum_key_must_be_a_string_in_its_enum(value) -> None:
    rig = Rig()
    result = rig.apply(patch_configmap({"log_level": value}, name="orders-config"))
    assert _shape(result) == (S.REFUSED_BY_ENVELOPE, []) and "is not one of" in result.error


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("db-pool-config", {"max_pool_size": 90}),  # outside safe 5..30, inside hard 1..100
        ("db-pool-config", {"max_pool_size": 1, "connection_timeout_ms": 30000}),  # the hard edges
        ("orders-config", {"log_level": "debug"}),
    ],
    ids=["safe-only-violation", "hard-edges", "enum-member"],
)
def test_configmap_within_the_envelope_executes(name, values) -> None:
    result = Rig().apply(patch_configmap(values, name=name))
    assert _shape(result) == (
        S.APPLIED,
        [("patch_configmap", "applied"), ("restart_deployment", "applied")],
    )


RESOURCE_REFUSED = {
    "request-cpu-above": {"requests": {"cpu_millis": 8001}},
    "limit-cpu-below": {"limits": {"cpu_millis": 9}},
    "request-memory-below": {"requests": {"memory_mib": 63}},
    "limit-memory-above": {"limits": {"memory_mib": 9000}},
    "valid-request-bad-limit": {"requests": {"cpu_millis": 100}, "limits": {"memory_mib": 9000}},
}


@pytest.mark.parametrize("sections", RESOURCE_REFUSED.values(), ids=RESOURCE_REFUSED.keys())
def test_resource_hard_ranges_are_checked_alone_and_before_any_read(sections) -> None:
    rig = Rig(unavailable=99)  # the reader is down: a read-free refusal must not need it
    result = rig.apply(resources(**sections))
    assert _shape(result) == (S.REFUSED_BY_ENVELOPE, []) and "hard range" in result.error
    assert rig.untouched


def test_resources_outside_the_safe_range_and_request_above_limit_reach_the_writer() -> None:
    rig = Rig()
    result = rig.apply(resources(limits={"memory_mib": 6144}))  # safe 256..2048, hard 64..8192
    assert result.status is S.APPLIED
    # request > limit, each inside its hard range: never an envelope rule (D§12).
    rig = Rig(["invalid"])
    result = rig.apply(resources(requests={"memory_mib": 2048}, limits={"memory_mib": 1024}))
    assert _shape(result) == (S.REJECTED_INVALID_ACTION, [("patch_deployment_resources", "failed")])
    assert len(rig.writer.writes) == 1


def test_a_missing_container_is_refused_and_no_container_is_added() -> None:
    rig = Rig()
    result = rig.apply(resources(container="sidecar", limits={"cpu_millis": 200}))
    assert _shape(result) == (S.REFUSED_BY_ENVELOPE, [])
    assert result.error == "container sidecar does not exist in Deployment/orders"
    assert rig.writer.writes == [] and rig.writer.preflights == []
    names = [
        c["name"]
        for c in rig.cluster.get("Deployment", "orders")["spec"]["template"]["spec"]["containers"]
    ]
    assert names == ["orders"]


def test_the_fake_strategic_merge_would_add_a_container() -> None:
    """Why the check exists (D§11.2): the same request without it adds a container."""
    rig = Rig()
    request = rig.executor._plan_resources(
        resources(limits={"cpu_millis": 200}), TOPOLOGY.resource("Deployment", "orders")
    )[0]
    body = request.body()
    body["spec"]["template"]["spec"]["containers"][0]["name"] = "sidecar"
    sneaky = dataclasses.replace(request, body_json=json.dumps(body))
    rig.cluster.apply(sneaky)
    names = [
        c["name"]
        for c in rig.cluster.get("Deployment", "orders")["spec"]["template"]["spec"]["containers"]
    ]
    assert names == ["orders", "sidecar"]


def test_the_container_check_reader_unavailable_is_an_infrastructure_failure() -> None:
    rig = Rig(unavailable=READ_ATTEMPTS)
    result = rig.apply(resources(limits={"cpu_millis": 200}))
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, [])
    assert len(rig.reader.calls) == 3 and rig.sleeps == [2, 2]
    assert rig.writer.writes == [] and rig.writer.preflights == []


def test_the_container_check_recovers_within_its_attempts() -> None:
    rig = Rig(unavailable=2)
    assert rig.apply(resources(limits={"cpu_millis": 200})).status is S.APPLIED
    assert [c[0] for c in rig.reader.calls] == ["get", "get", "get"] and rig.sleeps == [2, 2]


def test_the_container_check_on_an_absent_deployment_refuses() -> None:
    cluster = FakeCluster()
    del cluster.objects[("Deployment", "orders")]
    rig = Rig(cluster=cluster)
    result = rig.apply(resources(limits={"cpu_millis": 200}))
    assert _shape(result) == (S.REFUSED_BY_ENVELOPE, []) and rig.writer.writes == []


def test_an_unreadable_container_list_is_an_infrastructure_failure_not_a_refusal() -> None:
    cluster = FakeCluster()
    cluster.objects[("Deployment", "orders")]["spec"]["template"]["spec"] = {}
    result = Rig(cluster=cluster).apply(resources(limits={"cpu_millis": 200}))
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, [])


# --- 4. the six exact requests -----------------------------------------------------------------


def _only_write(rig):
    (request,) = rig.writer.writes
    return request


def test_restart_request() -> None:
    rig = Rig()
    assert _shape(rig.apply(restart())) == (S.APPLIED, [("restart_deployment", "applied")])
    r = _only_write(rig)
    assert (r.operation, r.kind, r.namespace, r.name, r.patch_type) == (
        ActionType.RESTART_DEPLOYMENT,
        "Deployment",
        NS,
        "orders",
        PatchType.MERGE,
    )
    assert r.body() == {
        "spec": {
            "template": {
                "metadata": {"annotations": {RESTART_ANNOTATION: "2026-11-25T12:00:00.000000Z"}}
            }
        }
    }
    assert rig.reader.calls == []


def test_scale_request_patches_spec_replicas_never_the_scale_subresource() -> None:
    rig = Rig()
    rig.apply(scale(15))
    r = _only_write(rig)
    assert (r.kind, r.name, r.patch_type, r.body()) == (
        "Deployment",
        "orders",
        PatchType.MERGE,
        {"spec": {"replicas": 15}},
    )
    assert rig.reader.calls == []


def test_configmap_request_renders_values_and_keeps_unlisted_keys() -> None:
    rig = Rig()
    result = rig.apply(patch_configmap({"max_pool_size": 20, "connection_timeout_ms": 1500.5}))
    assert result.status is S.APPLIED
    patch, restart_request = rig.writer.writes
    assert (patch.kind, patch.name, patch.patch_type, patch.body()) == (
        "ConfigMap",
        "db-pool-config",
        PatchType.MERGE,
        {"data": {"max_pool_size": "20", "connection_timeout_ms": "1500.5"}},
    )
    assert (restart_request.operation, restart_request.kind, restart_request.name) == (
        ActionType.RESTART_DEPLOYMENT,
        "Deployment",
        "orders",  # the ConfigMap owner's one Deployment
    )
    assert rig.cluster.get("ConfigMap", "db-pool-config")["data"] == {
        "max_pool_size": "20",
        "connection_timeout_ms": "1500.5",
        "other": "kept",
    }
    assert rig.reader.calls == []


@pytest.mark.parametrize(
    ("value", "rendered"),
    [("warn", "warn"), (20, "20"), (0, "0"), (1500.5, "1500.5"), (0.1, "0.1"), (1e-07, "1e-07")],
)
def test_configmap_rendering(value, rendered) -> None:
    assert render_configmap_value(value) == rendered


def test_service_request_replaces_the_selector_exactly() -> None:
    rig = Rig()
    result = rig.apply(patch_service({"app": "inventory"}))  # fewer and different keys
    assert result.status is S.APPLIED
    r = _only_write(rig)
    assert (r.kind, r.name, r.patch_type, r.body()) == (
        "Service",
        "orders-svc",
        PatchType.JSON,
        [{"op": "replace", "path": "/spec/selector", "value": {"app": "inventory"}}],
    )
    service = rig.cluster.get("Service", "orders-svc")
    assert service["spec"]["selector"] == {"app": "inventory"}  # no stale "tier"
    assert service["spec"]["ports"] == [{"port": 80}]
    assert rig.reader.calls == []


def test_resources_request_patches_only_the_given_fields_in_kubernetes_units() -> None:
    rig = Rig()
    result = rig.apply(resources(requests={"cpu_millis": 250}, limits={"memory_mib": 1024}))
    assert result.status is S.APPLIED
    r = _only_write(rig)
    assert (r.kind, r.name, r.patch_type) == ("Deployment", "orders", PatchType.STRATEGIC)
    assert r.body() == {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "orders",
                            "resources": {
                                "requests": {"cpu": "250m"},
                                "limits": {"memory": "1024Mi"},
                            },
                        }
                    ]
                }
            }
        }
    }
    (container,) = rig.cluster.get("Deployment", "orders")["spec"]["template"]["spec"]["containers"]
    assert container["resources"] == {
        "requests": {"cpu": "250m", "memory": "256Mi"},  # untouched
        "limits": {"cpu": "500m", "memory": "1024Mi"},
    }
    assert [c[:3] for c in rig.reader.calls] == [("get", "Deployment", "orders")]


def test_rollback_request_is_the_revision_template_without_its_hash() -> None:
    rig = Rig()
    result = rig.apply(rollback(1))
    assert _shape(result) == (S.APPLIED, [("rollback_deployment", "applied")])
    r = _only_write(rig)
    assert (r.kind, r.name, r.patch_type) == ("Deployment", "orders", PatchType.JSON)
    expected = copy.deepcopy(seed_objects()[("ReplicaSet", "orders-rs1")]["spec"]["template"])
    del expected["metadata"]["labels"]["pod-template-hash"]
    assert r.body() == [{"op": "replace", "path": "/spec/template", "value": expected}]
    assert expected["metadata"]["annotations"] == {"team": "orders"}  # the rest is kept
    # The objects as read are unchanged: the hash label is still on what the reader returned.
    rs_items = [rep.obj for rep in rig.reader.replies if rep.obj and "items" in rep.obj][0]["items"]
    rs1 = next(i for i in rs_items if i["metadata"]["name"] == "orders-rs1")
    assert rs1["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] == "hash-orders-rs1"
    stored = rig.cluster.get("ReplicaSet", "orders-rs1")
    assert (
        stored["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] == "hash-orders-rs1"
    )
    assert [c[:2] for c in rig.reader.calls] == [("get", "Deployment"), ("list", "ReplicaSet")]
    assert all(c[4] == READ_TIMEOUT_S for c in rig.reader.calls)


def test_rollback_to_the_current_revision_is_permitted() -> None:
    assert Rig().apply(rollback(2)).status is S.APPLIED


@pytest.mark.parametrize(
    "revision",
    [9, 3, 4],
    ids=["not-in-history", "only-on-an-uncontrolled-replicaset", "only-on-another-uid"],
)
def test_rollback_to_a_revision_not_in_the_targets_history_is_refused(revision) -> None:
    rig = Rig()
    result = rig.apply(rollback(revision))
    assert _shape(result) == (S.REFUSED_BY_ENVELOPE, [])
    assert result.error == f"revision {revision} is not in Deployment/orders's history"
    assert rig.writer.writes == [] and rig.writer.preflights == []


def test_rollback_history_reader_unavailable_is_an_infrastructure_failure() -> None:
    rig = Rig(unavailable=[True, True, True])
    result = rig.apply(rollback(1))
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, []) and rig.writer.writes == []
    rig = Rig(unavailable=[False, True, True, True])  # the Deployment read, then the list fails
    result = rig.apply(rollback(1))
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, []) and rig.writer.writes == []
    assert len(rig.reader.calls) == 4 and rig.sleeps == [2, 2]


def test_rollback_with_two_replicasets_claiming_a_revision_cannot_be_established() -> None:
    cluster = FakeCluster()
    twin = copy.deepcopy(cluster.objects[("ReplicaSet", "orders-rs1")])
    twin["metadata"]["name"] = "orders-twin"
    twin["spec"]["template"]["spec"]["containers"][0]["image"] = "orders:other"
    cluster.objects[("ReplicaSet", "orders-twin")] = twin
    rig = Rig(cluster=cluster)
    assert _shape(rig.apply(rollback(1))) == (S.INFRASTRUCTURE_FAILURE, [])
    assert rig.writer.writes == []


def test_rollback_on_an_absent_deployment_is_refused() -> None:
    cluster = FakeCluster()
    del cluster.objects[("Deployment", "orders")]
    assert _shape(Rig(cluster=cluster).apply(rollback(1))) == (S.REFUSED_BY_ENVELOPE, [])


def test_provisional_rollback_template_equality_is_exact() -> None:
    """PROVISIONAL (B2 live server-defaulting proof): the frozen D§11.6 comparison. A
    returned or read-back template that differs at all, even by a server-defaulted field,
    does not establish the postcondition. Milestone 8B-2 verifies this against real
    defaulting and amends decision 0025 before any narrowing."""
    rig = Rig(["stale"])
    cluster = rig.cluster
    real_apply = cluster.apply

    def defaulting_apply(request):
        real_apply(request)
        template = cluster.objects[("Deployment", "orders")]["spec"]["template"]
        template["spec"]["terminationGracePeriodSeconds"] = 30  # a server default

    cluster.apply = defaulting_apply
    result = rig.apply(rollback(1))
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, [("rollback_deployment", "failed")])
    assert "absent" in result.error


# --- 5. restart values are unique ------------------------------------------------------------


def test_restart_values_are_unique_under_a_repeating_or_backward_clock() -> None:
    clock = Clock(T0, T0, T0 - timedelta(seconds=5), T0 + timedelta(seconds=1))
    rig = Rig(clock=clock)
    for _ in range(4):
        assert rig.apply(restart()).status is S.APPLIED
    values = [
        w.body()["spec"]["template"]["metadata"]["annotations"][RESTART_ANNOTATION]
        for w in rig.writer.writes
    ]
    assert values == [
        "2026-11-25T12:00:00.000000Z",
        "2026-11-25T12:00:00.000001Z",
        "2026-11-25T12:00:00.000002Z",
        "2026-11-25T12:00:01.000000Z",
    ]


def test_the_composite_restart_value_is_made_before_the_first_write_and_reconciled() -> None:
    rig = Rig(["success", "ambiguous"])
    result = rig.apply(patch_configmap({"max_pool_size": 20}))
    assert result.status is S.APPLIED
    assert rig.writer.clock_calls_at_write == [1, 1]  # generated once, before step 1 was sent
    restart_request = rig.writer.writes[1]
    value = restart_request.body()["spec"]["template"]["metadata"]["annotations"][
        RESTART_ANNOTATION
    ]
    assert restart_request.postcondition.expected() == value
    annotations = rig.cluster.get("Deployment", "orders")["spec"]["template"]["metadata"][
        "annotations"
    ]
    assert annotations[RESTART_ANNOTATION] == value


def test_a_naive_clock_is_a_harness_error_before_any_write() -> None:
    rig = Rig(clock=lambda: T0.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        rig.apply(restart())
    assert rig.writer.preflights == [] and rig.writer.writes == []


# --- 6. the ConfigMap's owner Deployment is unique, or it's a configuration defect ------------


POOL_CONFIG = {
    "kind": "ConfigMap",
    "approved_keys": {"max_pool_size": {"hard_range": [1, 100], "safe_range": [5, 30]}},
}
CANARY = {
    "kind": "Deployment",
    "owner": "orders",
    "replicas": {"hard_range": [0, 20], "safe_range": [2, 6]},
    "memory_mib": {"hard_range": [64, 8192], "safe_range": [256, 2048]},
    "cpu_millis": {"hard_range": [10, 8000], "safe_range": [100, 2000]},
}


@pytest.mark.parametrize(
    "extra",
    [
        {"db-pool-config": {**POOL_CONFIG, "owner": "postgres"}},  # a StatefulSet's service
        {"orders-canary": CANARY},
    ],
    ids=["no-owner-deployment", "two-owner-deployments"],
)
def test_a_configmap_without_exactly_one_owner_deployment_raises_before_any_write(extra) -> None:
    rig = Rig(topology=_topology_with(**extra))
    with pytest.raises(ExecutorConfigurationError, match="exactly one"):
        rig.apply(patch_configmap({"max_pool_size": 20}))
    assert rig.untouched


# --- 7. every request is preflighted before the first write ---------------------------------


def test_every_request_is_preflighted_together_before_the_first_write() -> None:
    rig = Rig()
    rig.apply(patch_configmap({"max_pool_size": 20}))
    ((requests, writes_before),) = rig.writer.preflights
    assert writes_before == 0
    assert [r.operation for r in requests] == [
        ActionType.PATCH_CONFIGMAP,
        ActionType.RESTART_DEPLOYMENT,
    ]
    assert tuple(rig.writer.writes) == requests


@pytest.mark.parametrize("act", [restart(), patch_configmap({"max_pool_size": 20}), rollback(1)])
def test_a_preflight_refusal_raises_and_nothing_is_written(act) -> None:
    rig = Rig(preflight_raises=PermissionError("verb outside the executor phase"))
    with pytest.raises(PermissionError):
        rig.apply(act)
    assert rig.writer.writes == []


# --- 8. write replies: single step ------------------------------------------------------------

SINGLE = {
    "success": (["success"], 0, S.APPLIED, "applied", 0),
    "invalid": (["invalid"], 0, S.REJECTED_INVALID_ACTION, "failed", 0),
    "definite-failure": (["definite"], 0, S.INFRASTRUCTURE_FAILURE, "failed", 0),
    "not-attempted": (["not_attempted"], 0, S.INFRASTRUCTURE_FAILURE, None, 0),
    "ambiguous-confirmed": (["ambiguous"], 0, S.APPLIED, "applied", 1),
    "ambiguous-absent": (["lost"], 0, S.INFRASTRUCTURE_FAILURE, "failed", 1),
    "ambiguous-unavailable": (["ambiguous"], 3, S.INFRASTRUCTURE_FAILURE, "failed", 3),
    "ambiguous-unavailable-then-confirmed": (["ambiguous"], 1, S.APPLIED, "applied", 2),
    "success-without-the-change-confirmed": (["stale"], 0, S.APPLIED, "applied", 1),
    "success-without-the-change-absent": (["lie"], 0, S.INFRASTRUCTURE_FAILURE, "failed", 1),
    "writer-raised-after-send": (["raise"], 0, S.APPLIED, "applied", 1),
    "writer-returned-junk": (["junk"], 0, S.APPLIED, "applied", 1),
}


@pytest.mark.parametrize(
    ("script", "down", "status", "step", "reads"), SINGLE.values(), ids=SINGLE.keys()
)
def test_a_single_write_reply_is_classified(script, down, status, step, reads) -> None:
    rig = Rig(script, unavailable=down)
    result = rig.apply(restart())
    assert result.status is status
    assert [s.status for s in result.steps] == ([] if step is None else [step])
    assert len(rig.writer.writes) == 1  # never a write retry
    assert len(rig.reader.calls) == reads
    assert rig.sleeps == [READ_RETRY_SLEEP_S] * min(down, reads - 1 if reads else 0)
    assert all(c[4] == READ_TIMEOUT_S for c in rig.reader.calls)
    if status is S.APPLIED:
        assert result.error is None and result.steps[0].error is None
    else:
        assert result.error and len(result.error) <= MAX_DISCARD_ERROR


def test_an_unresolved_reconciliation_says_so() -> None:
    result = Rig(["ambiguous"], unavailable=3).apply(restart())
    assert "unresolved" in result.error and result.steps[0].error == result.error


def test_reconciliation_makes_at_most_three_attempts_two_seconds_apart() -> None:
    for down, reads, sleeps in [
        (0, 1, []),
        (1, 2, [2]),
        (2, 3, [2, 2]),
        (3, 3, [2, 2]),
        (9, 3, [2, 2]),
    ]:
        rig = Rig(["ambiguous"], unavailable=down)
        rig.apply(scale(4))
        assert (len(rig.reader.calls), rig.sleeps) == (reads, sleeps), down


def test_a_successful_absent_read_ends_reconciliation() -> None:
    cluster = FakeCluster()
    rig = Rig(["lost"], cluster=cluster)
    original_get = cluster.get
    cluster.get = lambda kind, name: None  # the object is gone: ABSENT on the first read
    result = rig.apply(scale(4))
    cluster.get = original_get
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, [("scale_deployment", "failed")])
    assert len(rig.reader.calls) == 1 and rig.sleeps == []


@pytest.mark.parametrize(
    "act",
    [restart(), scale(4), patch_service({"app": "orders"}), patch_configmap({"max_pool_size": 20})],
)
def test_a_confirmed_success_reads_nothing(act) -> None:
    rig = Rig()
    assert rig.apply(act).status is S.APPLIED
    assert rig.reader.calls == []


def test_only_the_envelope_pre_reads_happen_on_a_confirmed_rollback_or_resources() -> None:
    rig = Rig()
    rig.apply(rollback(1))
    assert [c[0] for c in rig.reader.calls] == ["get", "list"]
    rig = Rig()
    rig.apply(resources(limits={"cpu_millis": 200}))
    assert [c[0] for c in rig.reader.calls] == ["get"]


# --- 9. the composite ---------------------------------------------------------------------------

COMPOSITE = {
    "both-applied": (["success", "success"], S.APPLIED, ["applied", "applied"]),
    "step1-invalid": (["invalid"], S.REJECTED_INVALID_ACTION, ["failed"]),
    "step1-definite": (["definite"], S.INFRASTRUCTURE_FAILURE, ["failed"]),
    "step1-not-attempted": (["not_attempted"], S.INFRASTRUCTURE_FAILURE, []),
    "step1-ambiguous-absent": (["lost"], S.INFRASTRUCTURE_FAILURE, ["failed"]),
    "step1-ambiguous-confirmed": (["ambiguous", "success"], S.APPLIED, ["applied", "applied"]),
    "step2-invalid": (["success", "invalid"], S.PARTIALLY_APPLIED, ["applied", "failed"]),
    "step2-definite": (["success", "definite"], S.PARTIALLY_APPLIED, ["applied", "failed"]),
    "step2-not-attempted": (
        ["success", "not_attempted"],
        S.PARTIALLY_APPLIED,
        ["applied", "skipped"],
    ),
    "step2-ambiguous-confirmed": (["success", "ambiguous"], S.APPLIED, ["applied", "applied"]),
    "step2-ambiguous-absent": (["success", "lost"], S.PARTIALLY_APPLIED, ["applied", "failed"]),
}


@pytest.mark.parametrize(("script", "status", "steps"), COMPOSITE.values(), ids=COMPOSITE.keys())
def test_composite_precedence(script, status, steps) -> None:
    rig = Rig(script)
    result = rig.apply(patch_configmap({"max_pool_size": 20}))
    assert (result.status, [s.status for s in result.steps]) == (status, steps)
    ops = [s.operation for s in result.steps]
    assert ops == [ActionType.PATCH_CONFIGMAP, ActionType.RESTART_DEPLOYMENT][: len(ops)]
    assert len(rig.writer.writes) == len(script)  # never a restart after an unapplied patch


def test_step2_unavailable_reconciliation_is_partial_and_unresolved() -> None:
    rig = Rig(["success", "ambiguous"], unavailable=3)
    result = rig.apply(patch_configmap({"max_pool_size": 20}))
    assert _shape(result) == (
        S.PARTIALLY_APPLIED,
        [("patch_configmap", "applied"), ("restart_deployment", "failed")],
    )
    assert "unresolved" in result.error and result.steps[0].error is None
    assert result.steps[1].error == result.error


def test_a_skipped_step_carries_no_error_but_the_execution_does() -> None:
    result = Rig(["success", "not_attempted"]).apply(patch_configmap({"max_pool_size": 20}))
    assert result.steps[1].status == "skipped" and result.steps[1].error is None
    assert "not attempted" in result.error


# --- 10. quantities: exact, binary is not decimal -------------------------------------------


@pytest.mark.parametrize(
    ("text", "millis"),
    [("1000m", 1000), ("1", 1000), ("500m", 500), ("0.5", 500), ("2.25", 2250), ("0", 0)],
)
def test_cpu_quantities(text, millis) -> None:
    assert quantity.parse_cpu(text) == millis


def test_cpu_equivalences() -> None:
    assert quantity.parse_cpu("1000m") == quantity.parse_cpu("1")
    assert quantity.parse_cpu("500m") == quantity.parse_cpu("0.5")


def test_memory_equivalences() -> None:
    p = quantity.parse_memory
    assert p("1024Mi") == p("1Gi")
    assert p("2048Mi") == p("2Gi")
    assert p("1073741824") == p("1Gi")
    assert p("1Gi") == 2**30 and p("1Mi") == 2**20


def test_decimal_memory_suffixes_are_not_binary() -> None:
    p = quantity.parse_memory
    assert p("1G") != p("1Gi") and p("1G") == 10**9
    assert p("1M") != p("1Mi") and p("1M") == 10**6
    assert p("1k") == 1000 and p("1Ki") == 1024
    assert p("1024Mi") != p("2Gi")
    assert not quantity.memory_is("1G", 1024) and quantity.memory_is("1Gi", 1024)
    assert not quantity.memory_is("1M", 1) and quantity.memory_is("1Mi", 1)


@pytest.mark.parametrize(
    "text", ["1e3", "+1", "-1", "1.5m", "1Ki", "1Gi", "100u", " 1", "1 ", "", "m", "1k"]
)
def test_unsupported_cpu_forms_establish_nothing(text) -> None:
    assert quantity.parse_cpu(text) is None


@pytest.mark.parametrize(
    "text", ["1e9", "500m", "1gi", "1Gib", "-1Gi", "1.5.0Mi", "", "Mi", "1 Mi"]
)
def test_unsupported_memory_forms_establish_nothing(text) -> None:
    assert quantity.parse_memory(text) is None


@pytest.mark.parametrize("value", [None, 1000, 1.0, ["1"], {"v": "1"}])
def test_a_quantity_that_is_not_a_string_establishes_nothing(value) -> None:
    assert quantity.parse_cpu(value) is None and quantity.parse_memory(value) is None
    assert not quantity.cpu_is(value, 1000) and not quantity.memory_is(value, 1)


def test_a_server_canonicalized_quantity_still_confirms_the_write() -> None:
    rig = Rig(cluster=FakeCluster(canonicalize=True))
    result = rig.apply(resources(requests={"cpu_millis": 1000}, limits={"memory_mib": 2048}))
    assert result.status is S.APPLIED and rig.reader.calls[1:] == []  # confirmed by response
    (c,) = rig.cluster.get("Deployment", "orders")["spec"]["template"]["spec"]["containers"]
    assert c["resources"]["requests"]["cpu"] == "1" and c["resources"]["limits"]["memory"] == "2Gi"


def test_an_unrecognized_returned_quantity_is_reconciled_never_a_crash() -> None:
    rig = Rig()
    real_apply = rig.cluster.apply

    def odd_units(request):
        real_apply(request)
        (c,) = rig.cluster.objects[("Deployment", "orders")]["spec"]["template"]["spec"][
            "containers"
        ]
        c["resources"]["limits"]["memory"] = "2147483648e0"

    rig.cluster.apply = odd_units
    result = rig.apply(resources(limits={"memory_mib": 2048}))
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, [("patch_deployment_resources", "failed")])
    assert len(rig.reader.calls) == 2  # the container check, then one reconciliation read


# --- 11. result types: the producer shapes can't be misstated --------------------------------


def _st(op, status, error=None):
    return ExecutionStepResult(ActionType(op), status, error)


GOOD_RESULTS = [
    (S.APPLIED, None, (_st("scale_deployment", "applied"),)),
    (S.APPLIED, None, (_st("patch_configmap", "applied"), _st("restart_deployment", "applied"))),
    (
        S.PARTIALLY_APPLIED,
        "x",
        (_st("patch_configmap", "applied"), _st("restart_deployment", "failed", "x")),
    ),
    (
        S.PARTIALLY_APPLIED,
        "x",
        (_st("patch_configmap", "applied"), _st("restart_deployment", "skipped")),
    ),
    (S.REFUSED_BY_ENVELOPE, "x", ()),
    (S.REJECTED_INVALID_ACTION, "x", (_st("patch_configmap", "failed", "x"),)),
    (S.INFRASTRUCTURE_FAILURE, "x", ()),
    (S.INFRASTRUCTURE_FAILURE, "x", (_st("scale_deployment", "failed", "x"),)),
]
BAD_RESULTS = [
    (S.APPLIED, None, ()),
    (S.APPLIED, "x", (_st("scale_deployment", "applied"),)),
    (S.APPLIED, None, (_st("scale_deployment", "failed", "x"),)),
    (
        S.PARTIALLY_APPLIED,
        "x",
        (_st("scale_deployment", "applied"), _st("scale_deployment", "failed", "x")),
    ),
    (
        S.PARTIALLY_APPLIED,
        "x",
        (_st("patch_configmap", "applied"), _st("restart_deployment", "applied")),
    ),
    (
        S.PARTIALLY_APPLIED,
        None,
        (_st("patch_configmap", "applied"), _st("restart_deployment", "skipped")),
    ),
    (S.REFUSED_BY_ENVELOPE, "x", (_st("scale_deployment", "skipped"),)),
    (S.REFUSED_BY_ENVELOPE, None, ()),
    (S.REJECTED_INVALID_ACTION, "x", ()),
    (
        S.REJECTED_INVALID_ACTION,
        "x",
        (_st("patch_configmap", "failed", "x"), _st("restart_deployment", "skipped")),
    ),
    (S.INFRASTRUCTURE_FAILURE, "x", (_st("scale_deployment", "applied"),)),
    (S.INFRASTRUCTURE_FAILURE, "", ()),
    (S.INFRASTRUCTURE_FAILURE, "x" * (MAX_DISCARD_ERROR + 1), ()),
]


@pytest.mark.parametrize(("status", "error", "steps"), GOOD_RESULTS)
def test_producer_shapes_construct(status, error, steps) -> None:
    result = ExecutionResult(status, error, steps)
    assert result.to_record()["status"] == status.value


@pytest.mark.parametrize(("status", "error", "steps"), BAD_RESULTS)
def test_misstated_shapes_cannot_be_built(status, error, steps) -> None:
    with pytest.raises(ValueError):
        ExecutionResult(status, error, steps)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("applied", "x"),
        ("skipped", "x"),
        ("failed", None),
        ("failed", ""),
        ("failed", "x" * 2001),
        ("unknown", None),
    ],
)
def test_step_error_text_appears_only_on_failed_steps(status, error) -> None:
    with pytest.raises(ValueError):
        ExecutionStepResult(ActionType.SCALE_DEPLOYMENT, status, error)


def test_error_texts_are_bounded_at_the_producer() -> None:
    long = "x" * 5000
    rig = Rig(["invalid"])
    rig.writer.write = lambda request: WriteReply(WriteClass.INVALID_ACTION, None, long)
    result = rig.apply(restart())
    assert (
        len(result.error) == MAX_DISCARD_ERROR and len(result.steps[0].error) == MAX_DISCARD_ERROR
    )
    refused = Rig().apply(patch_configmap({"log_level": "v" * 5000}, name="orders-config"))
    assert len(refused.error) == MAX_DISCARD_ERROR


def test_the_record_form() -> None:
    result = Rig(["success", "not_attempted"]).apply(patch_configmap({"max_pool_size": 20}))
    assert result.to_record() == {
        "status": "partially_applied",
        "error": result.error,
        "steps": [
            {"operation": "patch_configmap", "status": "applied", "error": None},
            {"operation": "restart_deployment", "status": "skipped", "error": None},
        ],
    }


# --- 12. transports: semantic and immutable --------------------------------------------------


def test_a_write_reply_is_detached_json_and_bounded() -> None:
    obj = {"spec": {"replicas": 3}}
    reply = WriteReply(WriteClass.SUCCESS, obj, "d" * 5000)
    obj["spec"]["replicas"] = 99
    assert reply.returned_object == {"spec": {"replicas": 3}} and len(reply.diagnostic) == 2000
    with pytest.raises(TypeError):
        WriteReply("success")
    with pytest.raises(ValueError):
        WriteReply(WriteClass.SUCCESS, {"x": float("nan")})
    with pytest.raises(ValueError):
        ReadReply(ReadOutcome.FOUND)
    with pytest.raises(ValueError):
        ReadReply(ReadOutcome.ABSENT, {"x": 1})


def test_a_prepared_write_is_immutable() -> None:
    rig = Rig()
    rig.apply(scale(4))
    request = rig.writer.writes[0]
    body = request.body()
    body["spec"]["replicas"] = 20
    assert request.body() == {"spec": {"replicas": 4}}
    with pytest.raises(AttributeError):
        request.name = "gateway"


def test_the_executor_needs_its_loaded_dependencies() -> None:
    rig = Rig()
    ok = dict(
        semantics=SEMANTICS, topology=TOPOLOGY, namespace=NS, reader=rig.reader, writer=rig.writer
    )
    ActionExecutor(**ok)
    for key, bad in [
        ("semantics", {}),
        ("topology", {}),
        ("namespace", "Test_Bed"),
        ("reader", object()),
        ("writer", object()),
    ]:
        with pytest.raises((TypeError, ValueError)):
            ActionExecutor(**{**ok, key: bad})


def _reader_bug(*args):
    raise RuntimeError("reader bug")


def test_a_reader_error_before_any_write_propagates() -> None:
    rig = Rig()
    rig.reader.get = _reader_bug
    with pytest.raises(RuntimeError, match="reader bug"):
        rig.apply(rollback(1))
    assert rig.writer.writes == []


def test_a_reader_error_after_a_write_is_an_unavailable_attempt() -> None:
    rig = Rig(["ambiguous"])
    rig.reader.get = _reader_bug
    result = rig.apply(scale(4))
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, [("scale_deployment", "failed")])
    assert "unresolved" in result.error and rig.sleeps == [2, 2]


# --- 13. acceptance: every ExecutionStatus through the real apply ----------------------------


def test_every_execution_status_is_reached_through_apply() -> None:
    reached = {
        Rig().apply(restart()).status,
        Rig(["success", "invalid"]).apply(patch_configmap({"max_pool_size": 20})).status,
        Rig().apply(scale(500)).status,
        Rig(["invalid"])
        .apply(resources(requests={"memory_mib": 2048}, limits={"memory_mib": 1024}))
        .status,
        Rig(["lost"]).apply(restart()).status,
    }
    assert reached == set(ExecutionStatus)


def test_action_semantics_and_target_kinds_come_from_the_loaded_table() -> None:
    assert SEMANTICS[ActionType.PATCH_CONFIGMAP].steps == (
        ActionType.PATCH_CONFIGMAP,
        ActionType.RESTART_DEPLOYMENT,
    )
    unusual = RecommendedAction(
        type=ActionType.PATCH_SERVICE,
        target=ActionTarget(kind="Deployment", ns=NS, name="orders"),
        params={"selector": {"app": "orders"}},
    )
    assert (
        Rig().apply(unusual).error == "E1: patch_service acts on a Service, not Deployment/orders"
    )


def test_a_selector_with_a_surviving_stale_key_is_not_confirmed() -> None:
    """A server that merged the selector instead of replacing it leaves `tier` behind: the
    exact-equality postcondition refuses to call that applied."""
    rig = Rig()
    real_apply = rig.cluster.apply

    def merging_apply(request):
        real_apply(request)
        rig.cluster.objects[("Service", "orders-svc")]["spec"]["selector"]["tier"] = "backend"

    rig.cluster.apply = merging_apply
    result = rig.apply(patch_service({"app": "inventory"}))
    assert _shape(result) == (S.INFRASTRUCTURE_FAILURE, [("patch_service", "failed")])
    assert "absent" in result.error


def test_the_read_bounds_are_the_frozen_values() -> None:
    assert (READ_ATTEMPTS, READ_TIMEOUT_S, READ_RETRY_SLEEP_S) == (3, 10, 2)
    rig = Rig(["ambiguous"], unavailable=3)
    rig.apply(restart())
    assert [c[4] for c in rig.reader.calls] == [10, 10, 10] and rig.sleeps == [2, 2]
