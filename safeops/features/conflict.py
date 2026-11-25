"""telemetry_conflict: the small deterministic rule seam (design §11.5, Rule 19; decision 0025
D§7.2).

A rule names one tool, one top-level scalar field of that tool's structured result, a
comparison from a closed set, and a literal of the field's type. It is evaluated over the
latest successful observation of that tool for the action's target (namespace = the alert's,
name = the target's; D§5). It fires only when that observation exists, holds the field with
the declared type, and the comparison is true. telemetry_conflict is 1.0 if any rule fires.

Deliberately not built in Milestone 8: cross-tool joins, nested field paths, expressions,
observation binding, tri-state evaluation. Milestone 12 may extend this deterministically
(D§7.2). No eval, no dynamic import, no LLM.

FIELDS is a closed table, written by hand from the frozen structured contracts in
safeops/tools/contracts.py (DeploymentData, RolloutHistoryData, ConfigMapData, ServiceData)
and pinned to them by tests/test_m8_features.py. It is not imported from there: any import of
safeops.tools initializes the kubectl-backed tool package (safeops.cluster).
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from safeops.domain import RecommendedAction, TrajectoryStep
from safeops.features.evidence import latest

# tool -> top-level scalar field -> its type. Optional (nullable) contract fields are listed
# with their non-null type; a stored null never satisfies a comparison.
FIELDS: Mapping[str, Mapping[str, type]] = MappingProxyType(
    {
        "get_deployment": MappingProxyType(
            {"found": bool, "current_revision": int, "replicas": int, "container_count": int}
        ),
        "get_rollout_history": MappingProxyType(
            {"revision_count": int, "total_revisions": int, "truncated": bool}
        ),
        "get_configmap": MappingProxyType(
            {"found": bool, "entry_count": int, "total_keys": int, "truncated": bool}
        ),
        "get_service": MappingProxyType(
            {"found": bool, "ready_endpoint_count": int, "endpoint_count": int}
        ),
    }
)
OPERATORS: Mapping[str, Callable[[object, object], bool]] = MappingProxyType(
    {
        "eq": operator.eq,
        "ne": operator.ne,
        "lt": operator.lt,
        "le": operator.le,
        "gt": operator.gt,
        "ge": operator.ge,
    }
)
ORDERING = frozenset({"lt", "le", "gt", "ge"})  # integer fields only


@dataclass(frozen=True)
class ConflictRule:
    id: str
    tool: str
    field: str
    operator: str
    value: bool | int


def fires(rule: ConflictRule, action: RecommendedAction, trajectory, namespace: str) -> bool:
    data = latest(trajectory, rule.tool, namespace, action.target.name)
    if data is None or rule.field not in data:
        return False
    observed = data[rule.field]
    if type(observed) is not FIELDS[rule.tool][rule.field]:
        return False  # null, a bool for an int, or any other malformed value: no evidence
    return bool(OPERATORS[rule.operator](observed, rule.value))


def telemetry_conflict(
    rules: Sequence[ConflictRule],
    action: RecommendedAction,
    trajectory: Sequence[TrajectoryStep],
    namespace: str,
) -> float:
    """1.0 if any rule fires over the trajectory, else 0.0 (any-fire, D§5.2)."""
    return 1.0 if any(fires(r, action, trajectory, namespace) for r in rules) else 0.0
