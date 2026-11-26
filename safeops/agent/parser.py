"""Proposal parser (design §11.4; decision 0025 D§3).

Turns the agent loop's AgentOutput into a ParsedProposal: a ProposalStatus and, for
valid_action and no_action, a Proposal.

- **How the agent finished** comes from the envelope's `termination`, never from the text.
- **An answer** must be exactly one strict JSON object. Duplicate keys at any level, the
  non-finite tokens and numbers that decode to infinity are refused while parsing, before
  any information is lost. Nothing is extracted, repaired, coerced or retried.
- **Structural only.** Action shapes come from the neutral contract module
  (safeops.actions). The hard envelope (namespace, whitelist, ranges, history) is the
  executor's, so a well-typed action it will refuse is still valid_action here.
- **Total over model content.** Anything the model wrote maps to a status; the parser
  never raises for it. Every field is validated before the domain models are built, so no
  domain ValidationError can escape from model content. A raise here is a harness bug: a
  wrong input type, or a violated internal invariant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from safeops.actions import ACTION_KEYS, exact_int, params_problem, target_problem
from safeops.agent.output import AgentOutput, Termination
from safeops.domain import ActionTarget, Proposal, RecommendedAction
from safeops.enums import ActionType, ProposalStatus
from safeops.jsonutil import MalformedJSON, strict_json_object

PROPOSAL_KEYS = frozenset(
    {"diagnosis", "recommended_action", "confidence", "evidence", "prerequisites_claimed"}
)
MAX_DIAGNOSIS_CHARS = 4096
MAX_PREREQUISITES = 32
MAX_PREREQUISITE_CHARS = 128

_WITH_PROPOSAL = frozenset({ProposalStatus.VALID_ACTION, ProposalStatus.NO_ACTION})
_ACTION_TYPES = {t.value: t for t in ActionType}


@dataclass(frozen=True)
class ParsedProposal:
    """The parser's result: a status and, for valid_action and no_action, the proposal."""

    status: ProposalStatus
    proposal: Proposal | None

    def __post_init__(self) -> None:
        if type(self.status) is not ProposalStatus:
            raise TypeError("ParsedProposal.status must be a ProposalStatus")
        if self.status in _WITH_PROPOSAL:
            if not isinstance(self.proposal, Proposal):
                raise ValueError(f"{self.status} carries its proposal")
            has_action = self.proposal.recommended_action is not None
            if has_action != (self.status is ProposalStatus.VALID_ACTION):
                raise ValueError(f"{self.status} contradicts its recommended_action")
        elif self.proposal is not None:
            raise ValueError(f"{self.status} carries no proposal")

    def to_record(self) -> dict:
        proposal = None if self.proposal is None else self.proposal.model_dump(mode="json")
        return {"proposal_status": self.status.value, "proposal": proposal}


# The strict JSON boundary is the neutral hardened parser (safeops.jsonutil), re-exported here
# so `safeops.agent.parser.strict_json_object` keeps working; _Malformed is its exception.
_Malformed = MalformedJSON


def _proposal(obj: dict, steps: int) -> Proposal | None:
    """The Proposal, or None if any field breaks its contract (D§3.3)."""
    if set(obj) != PROPOSAL_KEYS:
        return None
    diagnosis = obj["diagnosis"]
    if type(diagnosis) is not str or len(diagnosis) > MAX_DIAGNOSIS_CHARS:
        return None
    confidence = obj["confidence"]
    if not (exact_int(confidence) or type(confidence) is float):
        return None
    if not (math.isfinite(confidence) and 0 <= confidence <= 1):
        return None
    evidence = obj["evidence"]
    if type(evidence) is not list:
        return None
    if not all(exact_int(e) and 1 <= e <= steps for e in evidence):
        return None
    if len(set(evidence)) != len(evidence):
        return None
    claims = obj["prerequisites_claimed"]
    if type(claims) is not list or len(claims) > MAX_PREREQUISITES:
        return None
    if not all(type(c) is str and 1 <= len(c) <= MAX_PREREQUISITE_CHARS for c in claims):
        return None
    if len(set(claims)) != len(claims):
        return None
    action = None
    raw = obj["recommended_action"]
    if raw is not None:
        if type(raw) is not dict or set(raw) != ACTION_KEYS:
            return None
        action_type = _ACTION_TYPES.get(raw["type"]) if type(raw["type"]) is str else None
        if action_type is None or target_problem(raw["target"]) is not None:
            return None
        if params_problem(action_type, raw["params"]) is not None:
            return None
        action = RecommendedAction(
            type=action_type, target=ActionTarget(**raw["target"]), params=raw["params"]
        )
    return Proposal(
        diagnosis=diagnosis,
        recommended_action=action,
        confidence=float(confidence),
        evidence=list(evidence),
        prerequisites_claimed=list(claims),
    )


class ProposalParser:
    def parse(self, raw_output) -> ParsedProposal:
        """Turn the agent's output into a ParsedProposal (§7.1; decision 0025 D§3.2)."""
        if not isinstance(raw_output, AgentOutput):
            raise TypeError(f"ProposalParser.parse takes an AgentOutput, not {type(raw_output)}")
        termination = raw_output.termination
        if termination is Termination.PROVIDER_FAILURE:
            return ParsedProposal(ProposalStatus.PROVIDER_FAILURE, None)
        if termination is Termination.STEP_LIMIT:
            return ParsedProposal(ProposalStatus.STEP_LIMIT, None)
        if termination is Termination.REFUSAL or not raw_output.text.strip():
            return ParsedProposal(ProposalStatus.MODEL_REFUSAL, None)
        try:
            obj = strict_json_object(raw_output.text)
        except _Malformed:
            return ParsedProposal(ProposalStatus.INVALID_OUTPUT, None)
        proposal = _proposal(obj, raw_output.trajectory_steps)
        if proposal is None:
            return ParsedProposal(ProposalStatus.INVALID_OUTPUT, None)
        if proposal.recommended_action is None:
            return ParsedProposal(ProposalStatus.NO_ACTION, proposal)
        return ParsedProposal(ProposalStatus.VALID_ACTION, proposal)
