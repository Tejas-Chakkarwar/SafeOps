"""Fabricated Milestone 8A-2 inputs (Rule 17): stored tool results in their exact
record form, trajectory steps, proposals and runtime contexts.

Every builder produces what a run record stores: JSON-safe dicts inside the decision-0023
envelope, with the canonical digest. tests/test_m8_features.py checks each one against the
real contract validator (safeops.tools.contracts.step_problems), so the read path is
exercised on faithful evidence, never on a shape the tools can't produce.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from safeops.context import Alert, IncidentRuntimeContext
from safeops.domain import ActionTarget, Proposal, RecommendedAction, TrajectoryStep
from safeops.enums import ActionType
from safeops.tools.canonical import result_digest
from safeops.topology import load_topology_condition, parse_topology

NS = "testbed"
# One bound condition for the M8 tests, as build_runner has one for a run: the runner's
# context and the writer's ReadPathCheck must hold this very topology object.
CONDITION = load_topology_condition()
TOPOLOGY = CONDITION.topology
WHEN = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)


def context(target_service: str = "orders", topology=TOPOLOGY, namespace: str = NS):
    alert = Alert("HighErrorRate", namespace, target_service, "http_5xx_rate > 0.2 over 30s", WHEN)
    return IncidentRuntimeContext(alert=alert, topology=topology, now=WHEN)


def container(name: str = "app") -> dict:
    return {
        "name": name,
        "image": "registry.local/app:1",
        "env": [],
        "env_count": 0,
        "env_from": [],
        "requests": {"cpu": "100m", "memory": "256Mi"},
        "limits": {"memory": "512Mi"},
        "readiness_probe": None,
        "liveness_probe": None,
    }


def deployment(name: str, replicas: int | None = 2, *, containers=("app",), count=None) -> dict:
    if replicas is None:  # not found
        return {
            "namespace": NS,
            "name": name,
            "found": False,
            "current_revision": None,
            "replicas": None,
            "containers": None,
            "container_count": None,
        }
    listed = [container(c) for c in sorted(containers)]
    return {
        "namespace": NS,
        "name": name,
        "found": True,
        "current_revision": 2,
        "replicas": replicas,
        "containers": listed,
        "container_count": len(listed) if count is None else count,
    }


def revision(number: int, replicas: int, ready: int) -> dict:
    return {
        "revision": number,
        "replica_set": f"orders-r{number}",
        "created_at": "2026-11-24T17:00:00Z",
        "replicas": replicas,
        "ready_replicas": ready,
        "containers": [container()],
        "container_count": 1,
    }


def history(name: str, revisions: list[dict], total: int | None = None) -> dict:
    total = len(revisions) if total is None else total
    return {
        "namespace": NS,
        "name": name,
        "revisions": revisions,
        "revision_count": len(revisions),
        "total_revisions": total,
        "truncated": total > len(revisions),
    }


def entry(key: str, value: str | None) -> dict:
    if value is None:  # a secret-named key: redacted
        return {
            "key": key,
            "value": None,
            "length": None,
            "value_truncated": False,
            "redacted": True,
        }
    return {
        "key": key,
        "value": value,
        "length": len(value),
        "value_truncated": False,
        "redacted": False,
    }


def configmap(name: str, entries: dict[str, str | None], total: int | None = None) -> dict:
    listed = [entry(k, v) for k, v in sorted(entries.items())]
    total = len(listed) if total is None else total
    return {
        "namespace": NS,
        "name": name,
        "found": True,
        "entries": listed,
        "entry_count": len(listed),
        "total_keys": total,
        "truncated": total > len(listed),
        "binary_keys": [],
    }


def service(name: str, selector: dict | None = None, ready: int = 2, total: int = 2) -> dict:
    return {
        "namespace": NS,
        "name": name,
        "found": True,
        "selector": {"app": name.removesuffix("-svc")} if selector is None else selector,
        "ready_endpoint_count": ready,
        "endpoint_count": total,
    }


def ok(data: dict) -> dict:
    return {"status": "ok", "data": data}


def error(kind: str = "command_failed") -> dict:
    return {"status": "error", "error": {"kind": kind, "message": "the read failed"}}


def step(number: int, tool: str, name: str | None, result: dict, args: dict | None = None):
    """A stored step. `args` defaults to the named tools' {namespace, name}."""
    if args is None:
        args = {"namespace": NS} if name is None else {"namespace": NS, "name": name}
    return TrajectoryStep(
        step=number,
        tool=tool,
        args=args,
        result=result,
        result_digest=result_digest(result),
        tokens_in=100,
        tokens_out=10,
        latency_ms=12.5,
    )


def steps(*observations: tuple[str, str | None, dict]) -> list[TrajectoryStep]:
    """Number (tool, name, result) observations 1..n in order."""
    return [step(i, tool, name, result) for i, (tool, name, result) in enumerate(observations, 1)]


def pods_step(number: int = 1) -> TrajectoryStep:
    data = {"namespace": NS, "pods": [], "pod_count": 0, "total_pods": 0, "truncated": False}
    return step(number, "get_pods", None, ok(data))


def action(type_: str, kind: str, name: str, params: dict, ns: str = NS) -> RecommendedAction:
    return RecommendedAction(
        type=ActionType(type_), target=ActionTarget(kind=kind, ns=ns, name=name), params=params
    )


def proposal(act: RecommendedAction, *, confidence: float = 0.6, claims=(), evidence=()):
    return Proposal(
        diagnosis="fabricated",
        recommended_action=act,
        confidence=confidence,
        evidence=list(evidence),
        prerequisites_claimed=list(claims),
    )


def restart(name: str) -> RecommendedAction:
    return action("restart_deployment", "Deployment", name, {})


def scale(name: str, replicas: int) -> RecommendedAction:
    return action("scale_deployment", "Deployment", name, {"replicas": replicas})


def rollback(name: str = "orders", to: int = 1) -> RecommendedAction:
    return action("rollback_deployment", "Deployment", name, {"to_revision": to})


def resources(name: str, container_name: str = "app") -> RecommendedAction:
    return action(
        "patch_deployment_resources",
        "Deployment",
        name,
        {"container": container_name, "limits": {"memory_mib": 1024}},
    )


def patch_configmap(name: str, params: dict | None = None) -> RecommendedAction:
    return action("patch_configmap", "ConfigMap", name, params or {"max_pool_size": 20})


def patch_service(name: str, selector: dict | None = None) -> RecommendedAction:
    return action("patch_service", "Service", name, {"selector": selector or {"app": "orders"}})


def topology_with(extra_resources: dict) -> object:
    """The committed topology plus extra resources (for the non-unique workload cases)."""
    import yaml

    from safeops.topology import TOPOLOGY_PATH

    data = yaml.safe_load(TOPOLOGY_PATH.read_text())
    data = copy.deepcopy(data)
    data["resources"].update(extra_resources)
    return parse_topology(data)


DEPLOYMENT_BOUNDS = {
    "replicas": {"hard_range": [0, 20], "safe_range": [2, 6]},
    "memory_mib": {"hard_range": [64, 8192], "safe_range": [256, 2048]},
    "cpu_millis": {"hard_range": [10, 8000], "safe_range": [100, 2000]},
}
