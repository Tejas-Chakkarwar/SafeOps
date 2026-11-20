"""The design's frozen enums (§7.1, §9.1), defined once.

Neutral: no evaluator ground truth, no cluster access. The run-record schema (Milestone
5), the proposal parser and the executor (Milestone 8) all use these exact names. Never
rename a value: it is stored in finalized, immutable run records.
"""

from __future__ import annotations

from enum import StrEnum


class ProposalStatus(StrEnum):
    VALID_ACTION = "valid_action"  # parsed, known type, well-formed params
    NO_ACTION = "no_action"  # agent deliberately proposes no mutation
    INVALID_OUTPUT = "invalid_output"  # malformed JSON, unknown action type, bad params
    STEP_LIMIT = "step_limit"  # hit the cap without proposing
    MODEL_REFUSAL = "model_refusal"  # model declined or returned an empty answer
    PROVIDER_FAILURE = "provider_failure"  # LLM API unavailable after retries; not an agent outcome


class ExecutionStatus(StrEnum):
    APPLIED = "applied"
    PARTIALLY_APPLIED = "partially_applied"  # composite action, some steps succeeded
    REFUSED_BY_ENVELOPE = "refused_by_envelope"  # valid type, forbidden target/params
    REJECTED_INVALID_ACTION = "rejected_invalid_action"  # K8s rejected it, e.g. HTTP 422
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"  # cluster API down, timeout, harness bug


class ActionType(StrEnum):
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    RESTART_DEPLOYMENT = "restart_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"
    PATCH_CONFIGMAP = "patch_configmap"
    PATCH_SERVICE = "patch_service"
    PATCH_DEPLOYMENT_RESOURCES = "patch_deployment_resources"
