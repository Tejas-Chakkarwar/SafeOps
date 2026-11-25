"""Milestone 8A-2: the four policy files, loaded strictly, once, from exact bytes (decision
0025 D§7, D§7.1, D§7.2, D§7.3)."""

from __future__ import annotations

import hashlib
import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from safeops.domain import FeatureVector
from safeops.enums import ActionType
from safeops.features import conflict, evidence
from safeops.features.policy import (
    CONFLICT_RULES_FILE,
    EVIDENCE_REQUIREMENTS_FILE,
    FEATURE_NAMES,
    GATE_WEIGHTS_FILE,
    load_policy,
    parse_conflict_rules,
    parse_evidence_requirements,
    parse_gate_weights,
)
from safeops.policy import (
    ACTION_SEMANTICS_FILE,
    CONFIG_DIR,
    PolicyError,
    parse_action_semantics,
    read_policy_file,
)

A = ActionType
FILES = (ACTION_SEMANTICS_FILE, GATE_WEIGHTS_FILE, EVIDENCE_REQUIREMENTS_FILE, CONFLICT_RULES_FILE)

# --- the independent version -> weights regression (D§7.1) -------------------------------------
# Hand-written. Never computed from the current file: changing a weight in
# config/gate_weights.yaml without bumping gate_feature_version must fail here.
EXPECTED_WEIGHTS: dict[int, dict[str, str]] = {
    0: {
        "irreversibility": "0.125",
        "blast_radius": "0.125",
        "persistence_risk": "0.125",
        "scope_outside_target": "0.125",
        "confidence_deficit": "0.125",
        "evidence_insufficiency": "0.125",
        "prerequisites_unverified": "0.125",
        "telemetry_conflict": "0.125",
    },
}


def check_version_weights(weights) -> None:
    expected = EXPECTED_WEIGHTS.get(weights.gate_feature_version)
    assert expected is not None, f"unknown gate_feature_version {weights.gate_feature_version}"
    assert dict(weights.weights) == {k: Fraction(v) for k, v in expected.items()}


def test_the_committed_weights_are_their_versions_weights() -> None:
    weights = load_policy().gate_weights
    assert weights.gate_feature_version == 0
    check_version_weights(weights)


def test_a_changed_weight_under_the_same_version_fails_the_regression(tmp_path) -> None:
    text = (CONFIG_DIR / GATE_WEIGHTS_FILE).read_text()
    text = text.replace('irreversibility: "0.125"', 'irreversibility: "0.25"').replace(
        'blast_radius: "0.125"', 'blast_radius: "0"'
    )
    weights = parse_gate_weights(read_policy_file(_write(tmp_path, GATE_WEIGHTS_FILE, text)))
    assert weights.gate_feature_version == 0  # still loads: the sum is still exactly 1
    with pytest.raises(AssertionError):
        check_version_weights(weights)


def test_an_unknown_version_fails_the_regression(tmp_path) -> None:
    text = (
        (CONFIG_DIR / GATE_WEIGHTS_FILE)
        .read_text()
        .replace("gate_feature_version: 0", "gate_feature_version: 7")
    )
    weights = parse_gate_weights(read_policy_file(_write(tmp_path, GATE_WEIGHTS_FILE, text)))
    with pytest.raises(AssertionError, match="unknown gate_feature_version"):
        check_version_weights(weights)


# --- production values ------------------------------------------------------------------------


def test_the_action_semantics_table_is_design_9_2() -> None:
    sem = load_policy().action_semantics
    expected = {
        A.ROLLBACK_DEPLOYMENT: ("Deployment", "1.0", (A.ROLLBACK_DEPLOYMENT,)),
        A.RESTART_DEPLOYMENT: ("Deployment", "0.8", (A.RESTART_DEPLOYMENT,)),
        A.SCALE_DEPLOYMENT: ("Deployment", "0.7", (A.SCALE_DEPLOYMENT,)),
        A.PATCH_CONFIGMAP: ("ConfigMap", "0.5", (A.PATCH_CONFIGMAP, A.RESTART_DEPLOYMENT)),
        A.PATCH_SERVICE: ("Service", "0.6", (A.PATCH_SERVICE,)),
        A.PATCH_DEPLOYMENT_RESOURCES: ("Deployment", "0.4", (A.PATCH_DEPLOYMENT_RESOURCES,)),
    }
    assert set(sem.actions) == set(A)
    for action, (kind, rev, ops) in expected.items():
        assert (sem[action].target_kind, sem[action].reversibility, sem[action].steps) == (
            kind,
            Fraction(rev),
            ops,
        )


def test_the_evidence_requirements_are_d7_3() -> None:
    ev = load_policy().evidence
    assert dict(ev.requirements) == {
        A.ROLLBACK_DEPLOYMENT: (
            "rollout_history_retrieved",
            "prior_revision_health_observed",
            "failure_correlated_with_current_revision",
        ),
        A.RESTART_DEPLOYMENT: ("target_deployment_state_observed",),
        A.SCALE_DEPLOYMENT: ("current_replicas_observed",),
        A.PATCH_DEPLOYMENT_RESOURCES: ("target_container_resources_observed",),
        A.PATCH_CONFIGMAP: ("target_keys_observed",),
        A.PATCH_SERVICE: ("target_service_state_observed",),
    }
    assert ev.prerequisites == frozenset({"prior_revision_healthy"})


def test_the_production_conflict_rule_set_is_empty() -> None:
    assert load_policy().conflict.rules == ()


def test_feature_names_are_the_feature_vectors_fields_in_order() -> None:
    assert FEATURE_NAMES == tuple(FeatureVector.model_fields)


def test_every_registered_requirement_is_configured_under_its_own_action() -> None:
    configured = {
        rid: action for action, ids in load_policy().evidence.requirements.items() for rid in ids
    }
    assert configured == {rid: owner for rid, (owner, _) in evidence.REQUIREMENTS.items()}


# --- exact bytes, hashed once ---------------------------------------------------------------------


def test_each_hash_is_the_sha256_of_the_files_exact_bytes() -> None:
    p = load_policy().provenance()
    digest = {
        f: "sha256:" + hashlib.sha256((CONFIG_DIR / f).read_bytes()).hexdigest() for f in FILES
    }
    assert p == {
        "action_semantics_hash": digest[ACTION_SEMANTICS_FILE],
        "evidence_requirements_hash": digest[EVIDENCE_REQUIREMENTS_FILE],
        "conflict_rules_hash": digest[CONFLICT_RULES_FILE],
        "gate_feature_version": 0,
    }


def test_a_comment_changes_the_hash_but_not_the_values(tmp_path) -> None:
    """The hash is over bytes, never over a reserialized or normalized form."""
    path = _write(tmp_path, CONFLICT_RULES_FILE, (CONFIG_DIR / CONFLICT_RULES_FILE).read_text())
    before = parse_conflict_rules(read_policy_file(path))
    path.write_text(path.read_text() + "# a comment\n")
    after = parse_conflict_rules(read_policy_file(path))
    assert before.rules == after.rules == ()
    assert before.sha256 != after.sha256
    assert after.sha256 == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_loaded_policy_is_not_reread(tmp_path, monkeypatch) -> None:
    """Load once: changing (or deleting) the files afterwards changes nothing loaded."""
    from tests.m8_fixtures import context, pods_step, proposal, restart

    for f in FILES:
        shutil.copy(CONFIG_DIR / f, tmp_path / f)
    policy = load_policy(tmp_path)
    provenance = policy.provenance()
    from safeops.domain import FeatureVector
    from safeops.features.extractor import RiskFeatureExtractor
    from safeops.gate.shadow import ShadowGateScorer

    extractor, scorer = RiskFeatureExtractor(policy), ShadowGateScorer(policy)
    (tmp_path / GATE_WEIGHTS_FILE).write_text(
        (tmp_path / GATE_WEIGHTS_FILE).read_text().replace("version: 0", "version: 9")
    )
    (tmp_path / ACTION_SEMANTICS_FILE).unlink()
    reads: list[Path] = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or real(self))
    for _ in range(3):
        fv = extractor.extract(proposal(restart("orders")), [pods_step()], context())
        scorer.score(fv, run_id="0373adae-6334-41ef-a0f4-6d2d5a4fc04c")
    assert reads == []
    assert isinstance(fv, FeatureVector) and fv.irreversibility == pytest.approx(0.2)
    assert policy.provenance() == provenance and policy.gate_weights.gate_feature_version == 0


def test_the_policy_bundle_is_immutable() -> None:
    policy = load_policy()
    with pytest.raises(AttributeError):
        policy.gate_weights = None  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy.gate_weights.weights["blast_radius"] = Fraction(1)  # type: ignore[index]
    with pytest.raises(TypeError):
        policy.action_semantics.actions[A.SCALE_DEPLOYMENT] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        policy.evidence.requirements[A.SCALE_DEPLOYMENT] = ()  # type: ignore[index]


# --- the negative matrix -----------------------------------------------------------------------


def _write(tmp_path: Path, name: str, text: str | bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


PARSERS = {
    ACTION_SEMANTICS_FILE: parse_action_semantics,
    GATE_WEIGHTS_FILE: parse_gate_weights,
    EVIDENCE_REQUIREMENTS_FILE: parse_evidence_requirements,
    CONFLICT_RULES_FILE: parse_conflict_rules,
}


def _refused(tmp_path, name: str, text: str | bytes, match: str) -> None:
    with pytest.raises(PolicyError, match=match):
        PARSERS[name](read_policy_file(_write(tmp_path, name, text)))


def _edit(name: str, old: str, new: str) -> str:
    text = (CONFIG_DIR / name).read_text()
    assert old in text, old
    return text.replace(old, new, 1)


def test_a_missing_file_is_refused(tmp_path) -> None:
    with pytest.raises(PolicyError, match="cannot be read"):
        read_policy_file(tmp_path / "absent.yaml")
    with pytest.raises(PolicyError, match="cannot be read"):
        load_policy(tmp_path)


def test_invalid_utf8_is_refused(tmp_path) -> None:
    _refused(tmp_path, CONFLICT_RULES_FILE, b"status: pre-v1\nrules: []\n# \xff\n", "not UTF-8")


def test_invalid_yaml_is_refused(tmp_path) -> None:
    _refused(tmp_path, CONFLICT_RULES_FILE, "status: [pre-v1\nrules: []\n", "not valid YAML")


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (CONFLICT_RULES_FILE, "status: pre-v1\nstatus: pre-v1\nrules: []\n"),
        (
            GATE_WEIGHTS_FILE,
            _edit(
                GATE_WEIGHTS_FILE,
                '  blast_radius: "0.125"\n',
                '  blast_radius: "0.125"\n  blast_radius: "0.125"\n',
            ),
        ),
        (
            ACTION_SEMANTICS_FILE,
            _edit(
                ACTION_SEMANTICS_FILE,
                '    reversibility: "0.7"\n',
                '    reversibility: "0.7"\n    reversibility: "0.1"\n',
            ),
        ),
    ],
    ids=["top-level", "nested-weight", "nested-action"],
)
def test_a_duplicate_key_at_any_depth_is_refused(tmp_path, name, text) -> None:
    _refused(tmp_path, name, text, "duplicate key")


def test_a_merge_key_and_a_non_string_key_are_refused(tmp_path) -> None:
    _refused(
        tmp_path, CONFLICT_RULES_FILE, "base: &b {status: pre-v1}\n<<: *b\nrules: []\n", "merge"
    )
    _refused(tmp_path, CONFLICT_RULES_FILE, "status: pre-v1\nrules: []\n1: x\n", "not a string")


@pytest.mark.parametrize("name", FILES)
def test_a_wrong_top_level_type_is_refused(tmp_path, name) -> None:
    _refused(tmp_path, name, "- a list\n", "expected a mapping")


@pytest.mark.parametrize("name", FILES)
def test_an_unknown_top_level_key_is_refused(tmp_path, name) -> None:
    text = (CONFIG_DIR / name).read_text() + "extra: 1\n"
    _refused(tmp_path, name, text, r"unknown \['extra'\]")


@pytest.mark.parametrize(
    ("name", "key"),
    [
        (GATE_WEIGHTS_FILE, "gate_feature_version: 0\n"),
        (EVIDENCE_REQUIREMENTS_FILE, "status: pre-v1\n"),
        (CONFLICT_RULES_FILE, "rules: []\n"),
    ],
)
def test_a_missing_required_key_is_refused(tmp_path, name, key) -> None:
    _refused(tmp_path, name, _edit(name, key, ""), "missing")


@pytest.mark.parametrize(
    "name", (GATE_WEIGHTS_FILE, EVIDENCE_REQUIREMENTS_FILE, CONFLICT_RULES_FILE)
)
def test_a_wrong_status_is_refused(tmp_path, name) -> None:
    _refused(tmp_path, name, _edit(name, "status: pre-v1", "status: v1"), "status must be")


ACTION_SEMANTICS_CASES = {
    "missing-action": (
        '  patch_service:\n    target_kind: Service\n    reversibility: "0.6"\n'
        "    steps: [patch_service]\n",
        "",
        "missing actions",
    ),
    "extra-action": (
        "actions:\n",
        'actions:\n  delete_namespace:\n    target_kind: Namespace\n    reversibility: "0"\n'
        "    steps: [delete_namespace]\n",
        "unknown action type",
    ),
    "numeric-reversibility": ('reversibility: "0.8"', "reversibility: 0.8", "decimal string"),
    "exponent-reversibility": ('reversibility: "0.8"', 'reversibility: "8e-1"', "decimal string"),
    "invalid-decimal": ('reversibility: "0.8"', 'reversibility: "0.8.1"', "decimal string"),
    "reversibility-above-one": ('reversibility: "0.8"', 'reversibility: "1.5"', "outside"),
    "composite-one-step": (
        "steps: [patch_configmap, restart_deployment]",
        "steps: [patch_configmap]",
        "steps must be",
    ),
    "wrong-single-step": (
        "steps: [scale_deployment]",
        "steps: [restart_deployment]",
        "steps must be",
    ),
    "unknown-step": ("steps: [scale_deployment]", "steps: [scale]", "unknown action type"),
    "empty-steps": ("steps: [scale_deployment]", "steps: []", "steps must be"),
    "bad-kind": ("target_kind: Service", "target_kind: service", "not a Kubernetes kind"),
    "missing-field": ('    reversibility: "0.6"\n', "", "missing"),
}


@pytest.mark.parametrize(
    ("old", "new", "match"), ACTION_SEMANTICS_CASES.values(), ids=ACTION_SEMANTICS_CASES.keys()
)
def test_action_semantics_refusals(tmp_path, old, new, match) -> None:
    _refused(tmp_path, ACTION_SEMANTICS_FILE, _edit(ACTION_SEMANTICS_FILE, old, new), match)


GATE_WEIGHT_CASES = {
    "version-bool": ("gate_feature_version: 0", "gate_feature_version: true", "integer >= 0"),
    "version-negative": ("gate_feature_version: 0", "gate_feature_version: -1", "integer >= 0"),
    "version-float": ("gate_feature_version: 0", "gate_feature_version: 0.0", "integer >= 0"),
    "version-string": ("gate_feature_version: 0", 'gate_feature_version: "0"', "integer >= 0"),
    "missing-weight": ('  telemetry_conflict: "0.125"\n', "", "missing"),
    "extra-weight": (
        'telemetry_conflict: "0.125"',
        'telemetry_conflict: "0.125"\n  novelty: "0"',
        "unknown",
    ),
    "numeric-weight": ('blast_radius: "0.125"', "blast_radius: 0.125", "decimal string"),
    "negative-weight": ('blast_radius: "0.125"', 'blast_radius: "-0.125"', "decimal string"),
    "sum-above-one": ('blast_radius: "0.125"', 'blast_radius: "0.126"', "not exactly 1"),
    "sum-below-one": ('blast_radius: "0.125"', 'blast_radius: "0.124"', "not exactly 1"),
}


@pytest.mark.parametrize(
    ("old", "new", "match"), GATE_WEIGHT_CASES.values(), ids=GATE_WEIGHT_CASES.keys()
)
def test_gate_weight_refusals(tmp_path, old, new, match) -> None:
    _refused(tmp_path, GATE_WEIGHTS_FILE, _edit(GATE_WEIGHTS_FILE, old, new), match)


EVIDENCE_CASES = {
    "missing-action": (
        "  patch_service:\n    - target_service_state_observed\n",
        "",
        "missing requirement sets",
    ),
    "empty-set": (
        "  patch_service:\n    - target_service_state_observed\n",
        "  patch_service: []\n",
        "non-empty list",
    ),
    "unknown-id": (
        "    - target_service_state_observed\n",
        "    - service_was_queried\n",
        "not a requirement of patch_service",
    ),
    "id-under-wrong-action": (
        "    - current_replicas_observed\n",
        "    - target_deployment_state_observed\n",
        "not a requirement of scale_deployment",
    ),
    "duplicate-id": (
        "    - current_replicas_observed\n",
        "    - current_replicas_observed\n    - current_replicas_observed\n",
        "duplicate id",
    ),
    "unknown-action": (
        "requirements:\n",
        "requirements:\n  drain_node:\n    - current_replicas_observed\n",
        "unknown action type",
    ),
    "unknown-prerequisite": (
        "  - prior_revision_healthy\n",
        "  - prior_revision_healthy\n  - image_was_pulled\n",
        "unknown prerequisite ids",
    ),
    "wrong-type": (
        "    - current_replicas_observed\n",
        "    - 7\n",
        "ids are strings",
    ),
}


@pytest.mark.parametrize(
    ("old", "new", "match"), EVIDENCE_CASES.values(), ids=EVIDENCE_CASES.keys()
)
def test_evidence_requirement_refusals(tmp_path, old, new, match) -> None:
    text = _edit(EVIDENCE_REQUIREMENTS_FILE, old, new)
    _refused(tmp_path, EVIDENCE_REQUIREMENTS_FILE, text, match)


def _rules(*rules: str) -> str:
    return "status: pre-v1\nrules:\n" + "".join(rules)


RULE = (
    "  - {id: no-endpoints, tool: get_service, field: ready_endpoint_count,"
    " operator: eq, value: 0}\n"
)


def test_a_well_formed_rule_loads(tmp_path) -> None:
    loaded = parse_conflict_rules(
        read_policy_file(_write(tmp_path, CONFLICT_RULES_FILE, _rules(RULE)))
    )
    assert loaded.rules == (
        conflict.ConflictRule("no-endpoints", "get_service", "ready_endpoint_count", "eq", 0),
    )


CONFLICT_CASES = {
    "malformed-rule": ("  - just-a-string\n", "expected a mapping"),
    "unknown-key": (RULE.replace("value: 0}", "value: 0, why: x}"), "unknown"),
    "missing-key": (RULE.replace(", value: 0", ""), "missing"),
    "unknown-tool": (RULE.replace("get_service", "get_logs"), "unknown tool"),
    "unknown-field": (RULE.replace("ready_endpoint_count", "selector"), "no scalar field"),
    "unknown-operator": (RULE.replace("operator: eq", "operator: matches"), "unknown operator"),
    "bool-for-int": (RULE.replace("value: 0", "value: false"), "integer|int literal"),
    "string-for-int": (RULE.replace("value: 0", 'value: "0"'), "int literal"),
    "int-for-bool": (
        RULE.replace("ready_endpoint_count", "found").replace("value: 0", "value: 1"),
        "bool literal",
    ),
    "ordering-on-bool": (
        RULE.replace("ready_endpoint_count", "found")
        .replace("value: 0", "value: true")
        .replace("operator: eq", "operator: lt"),
        "integer fields only",
    ),
    "bad-id": (RULE.replace("no-endpoints", "No Endpoints"), "lowercase name"),
    "duplicate-id": (RULE + RULE, "duplicate rule id"),
}


@pytest.mark.parametrize(("rules", "match"), CONFLICT_CASES.values(), ids=CONFLICT_CASES.keys())
def test_conflict_rule_refusals(tmp_path, rules, match) -> None:
    _refused(tmp_path, CONFLICT_RULES_FILE, _rules(rules), match)


def test_rules_must_be_a_list(tmp_path) -> None:
    _refused(tmp_path, CONFLICT_RULES_FILE, "status: pre-v1\nrules: {}\n", "must be a list")
