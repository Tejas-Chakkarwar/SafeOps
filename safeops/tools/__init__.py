"""Read-only diagnostic tools with bounded outputs (design §11.3; Milestone 6, decision 0023).

The eight §11.3 tools, as plain functions taking a ToolContext:

    get_pods(namespace)                                   status only
    get_events(namespace, last_minutes)                   at most 50
    get_logs(service, last_minutes, severity, max_lines)  at most 200 lines, hard cap
    get_metrics(service, metric, window)                  at most 60 points
    get_deployment(namespace, name)                       spec + current revision
    get_rollout_history(namespace, name)                  last 5 revisions, from ReplicaSets
    get_configmap(namespace, name)                        content, secrets redacted
    get_service(namespace, name)                          selector + ready endpoint count

Read-only is enforced by the client identity, safeops-reader (Phase.READER and its RBAC),
never by a prompt. Every result is projected, deterministically ordered, bounded, JSON-safe,
and digested canonically (safeops.tools.canonical); every failed read is a ToolError, never
an empty result (safeops.tools.contracts).

The agent's senses, so protected like the agent itself.
Information wall (§6; decisions 0020, 0021): this package never reaches evaluator or
harness state: nothing under safeops.faults, safeops.experiment, safeops.verify,
safeops.environment, safeops.bootstrap or safeops.analysis, directly, transitively or
lazily. It may depend on neutral runtime modules, for example safeops.cluster (the guarded
client), safeops.context, safeops.domain, safeops.enums and safeops.topology; that list is
illustrative, the boundary is what's enforced, by tests/test_information_wall.py. Its outputs
are checked too: no harness object (Chaos Mesh resources or their events) reaches a result.
"""

from safeops.tools.kube import (
    get_configmap,
    get_deployment,
    get_events,
    get_logs,
    get_pods,
    get_rollout_history,
    get_service,
)
from safeops.tools.metrics import get_metrics

__all__ = [
    "get_configmap",
    "get_deployment",
    "get_events",
    "get_logs",
    "get_metrics",
    "get_pods",
    "get_rollout_history",
    "get_service",
]
