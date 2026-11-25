"""Risk feature extractor (design §10.3, §11.5; decision 0025 D§5, D§7.3).

`extract(proposal, trajectory, context)` and nothing else. No cluster client, no snapshot, no
clock read and no file read during extraction: the policy was loaded once, before, and is
passed in at construction. The live-cluster evidence is the trajectory, the bounded tool
results the agent read through safeops-reader, as the record stores them (D§5).

Inputs:
- `proposal`: the domain `safeops.domain.Proposal` of a valid_action (never the parser's
  ParsedProposal), with a structurally valid action (safeops.actions);
- `trajectory`: `TrajectoryStep`s, read in their stored form (evidence.stored_form): the
  record's JSON round trip, so the record always reproduces what extraction saw;
- `context`: `IncidentRuntimeContext`; only `alert` and `topology` are read, never `now`.

Total and pure: for every such proposal and any trajectory content it returns a
FeatureVector with eight finite values in [0, 1]. Missing, failed, irrelevant or malformed
evidence lowers no risk and raises no exception; only a misuse of this contract (a wrong
argument type) raises. No LLM computes anything here (Rule 19).

    irreversibility           1 - reversibility[type]                (action_semantics.yaml)
    blast_radius              safeops.features.blast                 (D§5.3)
    persistence_risk          1 iff patch_configmap of a persistence_affecting ConfigMap;
                              1 for an unresolvable patch_configmap; else 0  (D§5.1, D§5.5)
    scope_outside_target      1 iff owner_of(target) not in incident_scope(alert.target_service);
                              1 when unresolvable                    (the harm predicate, §11.7)
    confidence_deficit        1.0 - proposal.confidence              (float arithmetic)
    evidence_insufficiency    1 - established / required             (evidence_requirements.yaml)
    prerequisites_unverified  unsupported / claimed, 0 with no claims
    telemetry_conflict        1 iff any conflict rule fires          (conflict_rules.yaml)
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

from safeops.actions import params_problem, target_problem
from safeops.context import IncidentRuntimeContext
from safeops.domain import FeatureVector, Proposal, RecommendedAction, TrajectoryStep
from safeops.enums import ActionType
from safeops.features import evidence
from safeops.features.blast import BlastEvidence, blast_radius, classify_blast, resolvable
from safeops.features.conflict import telemetry_conflict
from safeops.features.policy import PolicyBundle


class RiskFeatureExtractor:
    def __init__(self, policy: PolicyBundle) -> None:
        if not isinstance(policy, PolicyBundle):
            raise TypeError(f"RiskFeatureExtractor needs a PolicyBundle, not {type(policy)}")
        self.policy = policy

    def extract(self, proposal, trajectory, context) -> FeatureVector:
        """Compute and freeze the FeatureVector, even under always-act. Never uses an LLM."""
        action = _action(proposal)
        steps = _steps(trajectory)
        if not isinstance(context, IncidentRuntimeContext):
            raise TypeError(f"extract takes an IncidentRuntimeContext, not {type(context)}")
        policy = self.policy
        namespace = context.alert.namespace
        resolved = resolvable(action, context, policy.action_semantics)
        return FeatureVector(
            irreversibility=float(1 - policy.action_semantics[action.type].reversibility),
            blast_radius=blast_radius(action, self.blast_evidence(proposal, steps, context)),
            persistence_risk=_persistence(action, context, resolved),
            scope_outside_target=_scope(action, context, resolved),
            confidence_deficit=1.0 - proposal.confidence,
            evidence_insufficiency=self._insufficiency(action, steps, namespace),
            prerequisites_unverified=self._unverified(proposal, action, steps, namespace),
            telemetry_conflict=telemetry_conflict(policy.conflict.rules, action, steps, namespace),
        )

    def blast_evidence(self, proposal, trajectory, context) -> BlastEvidence:
        """The blast-radius classifier for this proposal (D§5.3): its evidence categories."""
        return classify_blast(
            _action(proposal), _steps(trajectory), context, self.policy.action_semantics
        )

    def _insufficiency(self, action, steps, namespace) -> float:
        required = self.policy.evidence.requirements[action.type]
        present = sum(
            1 for rid in required if evidence.REQUIREMENTS[rid][1](action, steps, namespace)
        )
        return float(1 - Fraction(present, len(required)))

    def _unverified(self, proposal, action, steps, namespace) -> float:
        claims = proposal.prerequisites_claimed
        if not claims:
            return 0.0  # nothing unsupported was claimed (D§5.2)
        vocabulary = self.policy.evidence.prerequisites
        unsupported = sum(
            1
            for claim in claims
            if claim not in vocabulary
            or not evidence.PREREQUISITES[claim](action, steps, namespace)
        )
        return float(Fraction(unsupported, len(claims)))


def _action(proposal) -> RecommendedAction:
    if not isinstance(proposal, Proposal):
        raise TypeError(f"extract takes a domain Proposal, not {type(proposal)}")
    action = proposal.recommended_action
    if action is None:
        raise ValueError("extract takes a valid_action proposal; this one has no action")
    problem = target_problem(action.target.model_dump()) or params_problem(
        action.type, action.params
    )
    if problem is not None:
        raise ValueError(f"extract takes a structurally valid action: {problem}")
    return action


def _steps(trajectory) -> Sequence[TrajectoryStep]:
    return evidence.stored_form(trajectory)  # exactly what the record will hold


def _persistence(action, context, resolved: bool) -> float:
    if action.type is not ActionType.PATCH_CONFIGMAP:
        return 0.0
    if not resolved:
        return 1.0  # unknown whether persistence-affecting (D§5.1)
    spec = context.topology.resource(action.target.kind, action.target.name)
    return 1.0 if spec.persistence_affecting else 0.0


def _scope(action, context, resolved: bool) -> float:
    if not resolved:
        return 1.0  # not shown to be in scope (D§5.1)
    topology = context.topology
    owner = topology.owner_of(action.target.kind, action.target.name)
    return 0.0 if owner in topology.incident_scope(context.alert.target_service) else 1.0
