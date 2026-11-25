"""Milestone 8A-1: the proposal parser (design §11.4; decision 0025 D§3).

Fabricated AgentOutput envelopes only (Rule 17): no LLM. Every ProposalStatus is reached
from a real envelope through the real parser, never by constructing the enum. Model-written
content never raises: it is a status. The parser is structural only, so a well-typed action
the hard envelope will refuse is still valid_action (D§3.4).
"""

from __future__ import annotations

import copy
import json

import pytest

from safeops.agent.output import AgentOutput, Termination
from safeops.agent.parser import (
    MAX_DIAGNOSIS_CHARS,
    MAX_PREREQUISITE_CHARS,
    MAX_PREREQUISITES,
    ParsedProposal,
    ProposalParser,
    strict_json_object,
)
from safeops.domain import Proposal
from safeops.enums import ActionType, ProposalStatus

S = ProposalStatus
DEPLOYMENT = {"kind": "Deployment", "ns": "testbed", "name": "orders"}
ACTIONS = {
    "rollback_deployment": {"to_revision": 1},
    "restart_deployment": {},
    "scale_deployment": {"replicas": 3},
    "patch_configmap": {"max_pool_size": 20},
    "patch_service": {"selector": {"app": "orders"}},
    "patch_deployment_resources": {
        "container": "orders",
        "requests": {"memory_mib": 512},
        "limits": {"memory_mib": 1024, "cpu_millis": 500},
    },
}
TARGETS = {
    "patch_configmap": {"kind": "ConfigMap", "ns": "testbed", "name": "db-pool-config"},
    "patch_service": {"kind": "Service", "ns": "testbed", "name": "orders-svc"},
}


def _action(type_: str = "scale_deployment", params=None, target=None) -> dict:
    return {
        "type": type_,
        "target": copy.deepcopy(target or TARGETS.get(type_, DEPLOYMENT)),
        "params": copy.deepcopy(ACTIONS[type_] if params is None else params),
    }


def _proposal(**over) -> dict:
    obj = {
        "diagnosis": "orders is starved of replicas under load",
        "recommended_action": _action(),
        "confidence": 0.7,
        "evidence": [1, 2],
        "prerequisites_claimed": ["prior_revision_healthy"],
    }
    obj.update(over)
    return obj


def _answer(content, steps: int = 3) -> AgentOutput:
    text = content if isinstance(content, str) else json.dumps(content)
    return AgentOutput(Termination.ANSWER, text, steps)


def _parse(output: AgentOutput) -> ParsedProposal:
    return ProposalParser().parse(output)


def _status(content, steps: int = 3) -> ProposalStatus:
    return _parse(_answer(content, steps)).status


# --- AgentOutput: a strict, frozen harness envelope -------------------------------------------


def test_agent_output_is_frozen() -> None:
    out = AgentOutput(Termination.ANSWER, "{}", 0)
    with pytest.raises(AttributeError):
        out.trajectory_steps = 1  # type: ignore[misc]


@pytest.mark.parametrize("steps", [True, 1.0, "1"], ids=["bool", "float", "str"])
def test_agent_output_trajectory_steps_is_an_exact_int(steps) -> None:
    with pytest.raises(TypeError):
        AgentOutput(Termination.ANSWER, "{}", steps)


def test_agent_output_refuses_negative_steps_and_a_plain_string_termination() -> None:
    with pytest.raises(ValueError):
        AgentOutput(Termination.ANSWER, "{}", -1)
    with pytest.raises(TypeError):
        AgentOutput("answer", "{}", 0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("termination", "text", "exc"),
    [
        (Termination.ANSWER, None, TypeError),
        (Termination.REFUSAL, None, TypeError),
        (Termination.STEP_LIMIT, "", ValueError),
        (Termination.PROVIDER_FAILURE, "503", ValueError),
    ],
)
def test_agent_output_text_follows_its_termination(termination, text, exc) -> None:
    with pytest.raises(exc):
        AgentOutput(termination, text, 0)


def test_the_parser_takes_only_an_agent_output() -> None:
    for wrong in ("{}", {"termination": "answer"}, None):
        with pytest.raises(TypeError):
            ProposalParser().parse(wrong)


# --- termination decides; the text is never scanned for it ------------------------------------


def test_provider_failure_comes_from_the_envelope() -> None:
    parsed = _parse(AgentOutput(Termination.PROVIDER_FAILURE, None, 2))
    assert parsed.status is S.PROVIDER_FAILURE and parsed.proposal is None


def test_step_limit_comes_from_the_envelope() -> None:
    parsed = _parse(AgentOutput(Termination.STEP_LIMIT, None, 15))
    assert parsed.status is S.STEP_LIMIT and parsed.proposal is None


def test_refusal_comes_from_the_envelope_even_with_valid_json_text() -> None:
    parsed = _parse(AgentOutput(Termination.REFUSAL, json.dumps(_proposal()), 3))
    assert parsed.status is S.MODEL_REFUSAL and parsed.proposal is None


@pytest.mark.parametrize("text", ["", "   ", "\n\t "], ids=["empty", "spaces", "mixed"])
def test_an_empty_answer_is_a_model_refusal(text) -> None:
    assert _status(text) is S.MODEL_REFUSAL


def test_refusal_words_in_an_answer_are_not_scanned() -> None:
    # Prose, whatever it says, is not one JSON object: invalid_output, never a refusal.
    for text in ("I refuse.", "Sorry, I cannot help.", "provider error 503"):
        assert _status(text) is S.INVALID_OUTPUT


# --- the six statuses, each from a real envelope ----------------------------------------------


def test_valid_action_and_its_record_form() -> None:
    parsed = _parse(_answer(_proposal()))
    assert parsed.status is S.VALID_ACTION
    assert parsed.proposal.recommended_action.type is ActionType.SCALE_DEPLOYMENT
    record = parsed.to_record()
    assert set(record) == {"proposal_status", "proposal"}
    assert record["proposal_status"] == "valid_action"
    assert record["proposal"]["recommended_action"] == _action()
    assert record["proposal"]["evidence"] == [1, 2]


def test_no_action_keeps_its_proposal() -> None:
    parsed = _parse(_answer(_proposal(recommended_action=None, prerequisites_claimed=[])))
    assert parsed.status is S.NO_ACTION
    assert parsed.proposal is not None and parsed.proposal.recommended_action is None
    assert parsed.to_record()["proposal"]["recommended_action"] is None


def test_invalid_output_carries_no_proposal() -> None:
    parsed = _parse(_answer("not json"))
    assert parsed.status is S.INVALID_OUTPUT
    assert parsed.to_record() == {"proposal_status": "invalid_output", "proposal": None}


def test_every_proposal_status_is_reached_by_a_real_envelope() -> None:
    reached = {
        _parse(_answer(_proposal())).status,
        _parse(_answer(_proposal(recommended_action=None))).status,
        _parse(_answer("{")).status,
        _parse(AgentOutput(Termination.STEP_LIMIT, None, 15)).status,
        _parse(AgentOutput(Termination.REFUSAL, "", 1)).status,
        _parse(AgentOutput(Termination.PROVIDER_FAILURE, None, 1)).status,
    }
    assert reached == set(ProposalStatus)


def test_parsed_proposal_refuses_a_contradictory_shape() -> None:
    p = Proposal(
        diagnosis="d",
        recommended_action=None,
        confidence=0.5,
        evidence=[],
        prerequisites_claimed=[],
    )
    with pytest.raises(ValueError):
        ParsedProposal(S.VALID_ACTION, p)  # valid_action without an action
    with pytest.raises(ValueError):
        ParsedProposal(S.INVALID_OUTPUT, p)  # invalid_output carries no proposal
    with pytest.raises(ValueError):
        ParsedProposal(S.NO_ACTION, None)
    with pytest.raises(TypeError):
        ParsedProposal("no_action", p)  # type: ignore[arg-type]


# --- strict JSON: one object, nothing extracted or repaired -----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "```json\n" + json.dumps(_proposal()) + "\n```",
        "Here is my answer: " + json.dumps(_proposal()),
        json.dumps(_proposal()) + " hope this helps",
        json.dumps(_proposal()) + json.dumps(_proposal()),
        json.dumps([_proposal()]),
        "null",
        '"a string"',
        "42",
    ],
    ids=["fence", "prefix", "suffix", "two-objects", "array", "null", "string", "number"],
)
def test_the_answer_must_be_exactly_one_json_object(text) -> None:
    assert _status(text) is S.INVALID_OUTPUT


def test_surrounding_whitespace_is_allowed() -> None:
    assert _status("\n  " + json.dumps(_proposal()) + "  \n") is S.VALID_ACTION


def test_duplicate_top_level_key() -> None:
    text = json.dumps(_proposal())[:-1] + ', "confidence": 0.9}'
    assert _status(text) is S.INVALID_OUTPUT


def test_duplicate_keys_at_every_nesting_level() -> None:
    good = json.dumps(_proposal())
    for original, duplicated in (
        ('"type": "scale_deployment"', '"type": "scale_deployment", "type": "restart_deployment"'),
        ('"name": "orders"', '"name": "orders", "name": "inventory"'),
        ('"replicas": 3', '"replicas": 3, "replicas": 4'),
    ):
        assert original in good
        assert _status(good.replace(original, duplicated, 1)) is S.INVALID_OUTPUT, duplicated


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_tokens_are_refused(token) -> None:
    text = json.dumps(_proposal()).replace('"confidence": 0.7', f'"confidence": {token}')
    assert _status(text) is S.INVALID_OUTPUT


def test_the_json_boundary_refuses_non_finite_values_itself() -> None:
    # Not left to later field checks: the boundary refuses them wherever they appear.
    for text in ('{"x": NaN}', '{"x": Infinity}', '{"x": [-Infinity]}'):
        with pytest.raises(ValueError):
            strict_json_object(text)


def test_the_json_boundary_refuses_numbers_that_overflow_to_infinity() -> None:
    for text in ('{"x": 1e400}', '{"x": -1e309}'):
        with pytest.raises(ValueError):
            strict_json_object(text)
    text = json.dumps(_proposal()).replace('"confidence": 0.7', '"confidence": 1e400')
    assert _status(text) is S.INVALID_OUTPUT


def test_absurd_json_never_raises() -> None:
    huge_int = '{"x": ' + "9" * 5000 + "}"  # beyond Python's int-digit limit
    deep = "[" * 100000 + "]" * 100000
    for text in (huge_int, deep, "{" * 5000):
        assert _status(text) is S.INVALID_OUTPUT


# --- the proposal object ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["diagnosis", "recommended_action", "confidence", "evidence", "prerequisites_claimed"],
)
def test_a_missing_proposal_key_is_invalid(key) -> None:
    obj = _proposal()
    del obj[key]
    assert _status(obj) is S.INVALID_OUTPUT


def test_an_unknown_proposal_key_is_invalid() -> None:
    assert _status(_proposal(rationale="extra")) is S.INVALID_OUTPUT


def test_diagnosis_bound_and_type() -> None:
    assert _status(_proposal(diagnosis="x" * MAX_DIAGNOSIS_CHARS)) is S.VALID_ACTION
    assert _status(_proposal(diagnosis="x" * (MAX_DIAGNOSIS_CHARS + 1))) is S.INVALID_OUTPUT
    assert _status(_proposal(diagnosis=7)) is S.INVALID_OUTPUT
    parsed = _parse(_answer(_proposal(diagnosis="")))  # accepted, kept as written
    assert parsed.status is S.VALID_ACTION and parsed.proposal.diagnosis == ""
    assert _parse(_answer(_proposal(diagnosis=" a "))).proposal.diagnosis == " a "


@pytest.mark.parametrize(
    "value",
    ["0.8", True, False, -0.1, 1.1, None],
    ids=["str", "true", "false", "neg", "over", "null"],
)
def test_confidence_must_be_a_number_in_the_unit_interval(value) -> None:
    assert _status(_proposal(confidence=value)) is S.INVALID_OUTPUT


def test_integer_confidence_zero_and_one_become_floats() -> None:
    for value in (0, 1):
        parsed = _parse(_answer(_proposal(confidence=value)))
        assert parsed.status is S.VALID_ACTION
        assert type(parsed.proposal.confidence) is float and parsed.proposal.confidence == value


@pytest.mark.parametrize(
    "evidence", [[True], [2.0], ["2"], 2, None], ids=["bool", "float", "str", "not-list", "null"]
)
def test_evidence_entries_are_exact_integers(evidence) -> None:
    assert _status(_proposal(evidence=evidence)) is S.INVALID_OUTPUT


def test_evidence_zero_is_refused() -> None:
    # The domain type accepts 0 (NonNegInt); the parser's 1-based bound is what refuses it,
    # so an agent citing step 0 is invalid_output, never a rejected record.
    assert _status(_proposal(evidence=[0]), steps=3) is S.INVALID_OUTPUT
    assert _status(_proposal(evidence=[1, 0]), steps=3) is S.INVALID_OUTPUT


def test_evidence_above_the_declared_steps_is_refused() -> None:
    assert _status(_proposal(evidence=[3]), steps=3) is S.VALID_ACTION
    assert _status(_proposal(evidence=[4]), steps=3) is S.INVALID_OUTPUT
    assert _status(_proposal(evidence=[1]), steps=0) is S.INVALID_OUTPUT


def test_duplicate_evidence_is_refused() -> None:
    assert _status(_proposal(evidence=[1, 1]), steps=3) is S.INVALID_OUTPUT


def test_evidence_keeps_its_order_and_may_be_empty() -> None:
    assert _parse(_answer(_proposal(evidence=[3, 1, 2]))).proposal.evidence == [3, 1, 2]
    assert _status(_proposal(evidence=[]), steps=0) is S.VALID_ACTION


def test_prerequisites_claimed() -> None:
    assert _status(_proposal(prerequisites_claimed="x")) is S.INVALID_OUTPUT
    assert _status(_proposal(prerequisites_claimed=[3])) is S.INVALID_OUTPUT
    assert _status(_proposal(prerequisites_claimed=[""])) is S.INVALID_OUTPUT
    assert _status(_proposal(prerequisites_claimed=["a", "a"])) is S.INVALID_OUTPUT
    at = "c" * MAX_PREREQUISITE_CHARS
    assert _status(_proposal(prerequisites_claimed=[at])) is S.VALID_ACTION
    assert _status(_proposal(prerequisites_claimed=[at + "c"])) is S.INVALID_OUTPUT
    many = [f"c{i}" for i in range(MAX_PREREQUISITES)]
    assert _status(_proposal(prerequisites_claimed=many)) is S.VALID_ACTION
    assert _status(_proposal(prerequisites_claimed=[*many, "x"])) is S.INVALID_OUTPUT


def test_unknown_prerequisite_ids_are_parser_valid_and_kept_as_written() -> None:
    parsed = _parse(_answer(_proposal(prerequisites_claimed=["Made Up Claim ", "x"])))
    assert parsed.status is S.VALID_ACTION
    assert parsed.proposal.prerequisites_claimed == ["Made Up Claim ", "x"]


# --- the action object and its target ---------------------------------------------------------


@pytest.mark.parametrize("key", ["type", "target", "params"])
def test_a_missing_action_key_is_invalid(key) -> None:
    action = _action()
    del action[key]
    assert _status(_proposal(recommended_action=action)) is S.INVALID_OUTPUT


def test_an_unknown_action_key_or_type_is_invalid() -> None:
    assert _status(_proposal(recommended_action={**_action(), "why": 1})) is S.INVALID_OUTPUT
    for t in ("delete_namespace", "Scale_Deployment", 3, None):
        assert _status(_proposal(recommended_action={**_action(), "type": t})) is S.INVALID_OUTPUT


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "Deployment", "ns": "testbed"},
        {**DEPLOYMENT, "uid": "x"},
        {**DEPLOYMENT, "kind": "deployment"},
        {**DEPLOYMENT, "kind": "Deploy-ment"},
        {**DEPLOYMENT, "ns": "Testbed"},
        {**DEPLOYMENT, "ns": "a.b"},
        {**DEPLOYMENT, "name": "../orders"},
        {**DEPLOYMENT, "name": "Orders"},
        {**DEPLOYMENT, "name": 5},
        "Deployment/orders",
    ],
    ids=[
        "missing",
        "unknown",
        "kind-case",
        "kind-char",
        "ns-case",
        "ns-dot",
        "name-path",
        "name-case",
        "name-type",
        "not-object",
    ],
)
def test_target_grammar(target) -> None:
    assert _status(_proposal(recommended_action=_action(target=target))) is S.INVALID_OUTPUT


# --- the six structural parameter contracts ---------------------------------------------------


@pytest.mark.parametrize("type_", list(ACTIONS))
def test_every_action_type_parses(type_) -> None:
    parsed = _parse(_answer(_proposal(recommended_action=_action(type_))))
    assert parsed.status is S.VALID_ACTION
    assert parsed.proposal.recommended_action.type is ActionType(type_)
    assert parsed.to_record()["proposal"]["recommended_action"] == _action(type_)


def test_the_contracts_cover_every_action_type() -> None:
    assert set(ACTIONS) == {t.value for t in ActionType}


BAD_PARAMS = {
    "rollback-missing": ("rollback_deployment", {}),
    "rollback-zero": ("rollback_deployment", {"to_revision": 0}),
    "rollback-bool": ("rollback_deployment", {"to_revision": True}),
    "rollback-float": ("rollback_deployment", {"to_revision": 2.0}),
    "rollback-str": ("rollback_deployment", {"to_revision": "2"}),
    "rollback-extra": ("rollback_deployment", {"to_revision": 2, "x": 1}),
    "restart-params": ("restart_deployment", {"at": "now"}),
    "scale-missing": ("scale_deployment", {}),
    "scale-bool": ("scale_deployment", {"replicas": True}),
    "scale-float": ("scale_deployment", {"replicas": 15.0}),
    "scale-str": ("scale_deployment", {"replicas": "15"}),
    "configmap-empty": ("patch_configmap", {}),
    "configmap-too-many": ("patch_configmap", {f"k{i}": 1 for i in range(65)}),
    "configmap-bad-key": ("patch_configmap", {"bad key": 1}),
    "configmap-bool": ("patch_configmap", {"max_pool_size": True}),
    "configmap-null": ("patch_configmap", {"max_pool_size": None}),
    "configmap-list": ("patch_configmap", {"max_pool_size": [20]}),
    "configmap-object": ("patch_configmap", {"max_pool_size": {"v": 20}}),
    "service-missing": ("patch_service", {}),
    "service-empty": ("patch_service", {"selector": {}}),
    "service-too-many": ("patch_service", {"selector": {f"k{i}": "v" for i in range(33)}}),
    "service-bad-value": ("patch_service", {"selector": {"app": "a b"}}),
    "service-bad-key": ("patch_service", {"selector": {"-app": "x"}}),
    "service-non-str": ("patch_service", {"selector": {"app": 1}}),
    "service-extra": ("patch_service", {"selector": {"app": "x"}, "ports": []}),
    "resources-no-container": ("patch_deployment_resources", {"requests": {"memory_mib": 1}}),
    "resources-bad-container": (
        "patch_deployment_resources",
        {"container": "Orders", "requests": {"memory_mib": 1}},
    ),
    "resources-nothing": ("patch_deployment_resources", {"container": "orders"}),
    "resources-empty": ("patch_deployment_resources", {"container": "orders", "limits": {}}),
    "resources-unknown-field": (
        "patch_deployment_resources",
        {"container": "orders", "limits": {"memory": 1}},
    ),
    "resources-bool": (
        "patch_deployment_resources",
        {"container": "orders", "limits": {"memory_mib": True}},
    ),
    "resources-str": (
        "patch_deployment_resources",
        {"container": "orders", "limits": {"memory_mib": "512Mi"}},
    ),
    "resources-removal": (
        "patch_deployment_resources",
        {"container": "orders", "limits": {"memory_mib": None}},
    ),
    "resources-extra": (
        "patch_deployment_resources",
        {"container": "orders", "limits": {"memory_mib": 1}, "image": "x"},
    ),
}


@pytest.mark.parametrize(("type_", "params"), BAD_PARAMS.values(), ids=BAD_PARAMS.keys())
def test_malformed_params_are_invalid_output(type_, params) -> None:
    assert _status(_proposal(recommended_action=_action(type_, params))) is S.INVALID_OUTPUT


def test_configmap_values_may_be_strings_integers_or_finite_floats() -> None:
    params = {"log_level": "debug", "max_pool_size": 20, "connection_timeout_ms": 2500.5}
    assert _status(_proposal(recommended_action=_action("patch_configmap", params))) is (
        S.VALID_ACTION
    )


def test_selector_labels_may_carry_a_prefix_and_an_empty_value() -> None:
    params = {"selector": {"app.kubernetes.io/name": "orders", "tier": ""}}
    assert _status(_proposal(recommended_action=_action("patch_service", params))) is (
        S.VALID_ACTION
    )


# --- structural, not envelope: well-typed actions M8B refuses stay valid_action ----------------

ENVELOPE_LATER = {
    "scale-negative": _action("scale_deployment", {"replicas": -1}),
    "scale-500": _action("scale_deployment", {"replicas": 500}),
    "restart-statefulset": _action(
        "restart_deployment", target={"kind": "StatefulSet", "ns": "testbed", "name": "postgres"}
    ),
    "kube-system": _action(target={**DEPLOYMENT, "ns": "kube-system"}),
    "unknown-deployment": _action(target={**DEPLOYMENT, "name": "checkout"}),
    "configmap-unknown-key": _action("patch_configmap", {"not_an_approved_key": "x"}),
    "configmap-out-of-hard-range": _action("patch_configmap", {"max_pool_size": 5000}),
    "request-above-limit": _action(
        "patch_deployment_resources",
        {"container": "orders", "requests": {"memory_mib": 2048}, "limits": {"memory_mib": 1024}},
    ),
    "nonexistent-container": _action(
        "patch_deployment_resources", {"container": "sidecar", "limits": {"cpu_millis": 100}}
    ),
}


@pytest.mark.parametrize("action", ENVELOPE_LATER.values(), ids=ENVELOPE_LATER.keys())
def test_envelope_invalid_but_well_typed_actions_stay_valid(action) -> None:
    assert _status(_proposal(recommended_action=action)) is S.VALID_ACTION
