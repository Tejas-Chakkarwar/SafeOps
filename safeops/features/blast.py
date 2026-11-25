"""blast_radius and its evidence classifier (decision 0025 D§5.1, D§5.3).

    replica_component   = clamp(R, 0, 20) / 20     replica evidence observed
                          1                        otherwise (this component only)
        R = c                    every action but scale_deployment
        R = max(c, requested)    scale_deployment (a requested count is never used alone)
        c = replicas of the latest successful matching get_deployment of the workload W
    dependent_component = D / D_max  (0 if D_max == 0)   resolvable target
                          1                              unresolvable target
        D     = |topology.dependents(owner)|
        D_max = max(|topology.dependents(s)| for s in topology.services)
    blast_radius        = (replica_component + dependent_component) / 2

Counts are combined exactly (Fraction) and converted to float once.

**The classifier is the provenance of the number.** `blast_radius == 1.0` is reached both by
observed evidence (inventory at 20 replicas) and by fail-closed values (inventory with no
replica evidence), so no analysis may infer evidence provenance from the scalar. It must use
`classify_blast`, the same pure function the extractor uses. It isn't persisted (no schema
field): it is recomputable from a stored record's proposal, trajectory, alert and topology.

**A workload that isn't unique is classified, never raised.** For patch_configmap and
patch_service, W is the one Deployment the target's owner owns. Zero or several gives
`no_deployment_workload` and replica_component 1, and extraction carries on: the extractor is
total over agent-visible conditions. (The executor's need for one restart target is a
separate, M8B matter: there a non-unique owner is a configuration defect, D§11.2.)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from safeops.context import IncidentRuntimeContext
from safeops.domain import RecommendedAction, TrajectoryStep
from safeops.enums import ActionType
from safeops.features.evidence import latest, observed_replicas
from safeops.policy import ActionSemantics

HARD_REPLICA_MAX = 20  # design §8's absolute 0-20 replica bound (every Deployment's hard_range)
_DEPLOYMENT_TARGETS = frozenset(
    {
        ActionType.ROLLBACK_DEPLOYMENT,
        ActionType.RESTART_DEPLOYMENT,
        ActionType.SCALE_DEPLOYMENT,
        ActionType.PATCH_DEPLOYMENT_RESOURCES,
    }
)


class ReplicaEvidence(StrEnum):
    OBSERVED = "observed"
    NO_REPLICA_EVIDENCE = "no_replica_evidence"  # recoverable: W could have been observed
    NO_DEPLOYMENT_WORKLOAD = "no_deployment_workload"  # structural: no unique Deployment
    UNRESOLVABLE_TARGET = "unresolvable_target"


class TopologyEvidence(StrEnum):
    OBSERVED = "observed"
    UNRESOLVABLE_TARGET = "unresolvable_target"


@dataclass(frozen=True)
class BlastEvidence:
    """What blast_radius was computed from (D§5.3's categories), and the counts it used."""

    replica: ReplicaEvidence
    topology: TopologyEvidence
    workload: str | None  # W, when unique
    replicas: int | None  # c, when observed
    dependents: int | None  # D, when the target resolved
    dependents_max: int


def resolvable(
    action: RecommendedAction, context: IncidentRuntimeContext, semantics: ActionSemantics
) -> bool:
    """D§5.1, explicitly: no classification ever depends on catching TopologyError."""
    target = action.target
    return (
        target.ns == context.alert.namespace
        and target.kind == semantics[action.type].target_kind
        and context.topology.resource(target.kind, target.name) is not None
    )


def _workload(action: RecommendedAction, context: IncidentRuntimeContext, owner: str) -> str | None:
    if action.type in _DEPLOYMENT_TARGETS:
        return action.target.name
    owned = [
        spec.name
        for spec in context.topology.resources.values()
        if spec.kind == "Deployment" and spec.owner == owner
    ]
    return owned[0] if len(owned) == 1 else None


def classify_blast(
    action: RecommendedAction,
    trajectory: Sequence[TrajectoryStep],
    context: IncidentRuntimeContext,
    semantics: ActionSemantics,
) -> BlastEvidence:
    topology = context.topology
    d_max = max((len(topology.dependents(s)) for s in topology.services), default=0)
    if not resolvable(action, context, semantics):
        return BlastEvidence(
            ReplicaEvidence.UNRESOLVABLE_TARGET,
            TopologyEvidence.UNRESOLVABLE_TARGET,
            None,
            None,
            None,
            d_max,
        )
    owner = topology.owner_of(action.target.kind, action.target.name)  # resolvable: never raises
    dependents = len(topology.dependents(owner))
    workload = _workload(action, context, owner)
    if workload is None:
        replica, count = ReplicaEvidence.NO_DEPLOYMENT_WORKLOAD, None
    else:
        data = latest(trajectory, "get_deployment", context.alert.namespace, workload)
        count = observed_replicas(data)
        replica = ReplicaEvidence.NO_REPLICA_EVIDENCE if count is None else ReplicaEvidence.OBSERVED
    return BlastEvidence(replica, TopologyEvidence.OBSERVED, workload, count, dependents, d_max)


def blast_radius(action: RecommendedAction, evidence: BlastEvidence) -> float:
    if evidence.replica is ReplicaEvidence.OBSERVED:
        r = evidence.replicas
        if action.type is ActionType.SCALE_DEPLOYMENT:
            r = max(r, action.params["replicas"])
        replica_component = Fraction(min(max(r, 0), HARD_REPLICA_MAX), HARD_REPLICA_MAX)
    else:
        replica_component = Fraction(1)
    if evidence.topology is TopologyEvidence.UNRESOLVABLE_TARGET:
        dependent_component = Fraction(1)
    elif evidence.dependents_max > 0:
        dependent_component = Fraction(evidence.dependents, evidence.dependents_max)
    else:
        dependent_component = Fraction(0)
    return float((replica_component + dependent_component) / 2)
