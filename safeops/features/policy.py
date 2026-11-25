"""The Milestone 8 feature and scoring policy: one immutable bundle, loaded once (decision 0025
D§7, D§7.1).

`load_policy()` reads the four policy files exactly once each (safeops.policy), validates them
strictly, and returns a frozen `PolicyBundle`. That one object is handed to the feature
extractor, the shadow scorer and the record writer (build_runner), so the values that
produce a feature vector and its scores and the provenance written beside them come from the
same bytes. Nothing re-reads a file afterwards: there is no hot reload; a changed file is a
new experimental condition, and needs a new process.

Provenance: the three file hashes and the declared integer gate_feature_version. There is
no gate-weights hash (D§7.1); tests/test_m8_policy.py pins each version to its weights.

The feature-side vocabularies live in this package, so Milestone 12's changes land inside
safeops/features/ and config/ only (D§7): requirement ids and prerequisite ids are the
predicate registries of safeops.features.evidence; conflict tools, fields and operators are
safeops.features.conflict's closed tables.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType

from safeops.enums import ActionType
from safeops.features import conflict, evidence
from safeops.policy import (
    ACTION_SEMANTICS_FILE,
    CONFIG_DIR,
    ActionSemantics,
    PolicyError,
    PolicyFile,
    action_type,
    decimal,
    parse_action_semantics,
    read_policy_file,
    require_keys,
)

GATE_WEIGHTS_FILE = "gate_weights.yaml"
EVIDENCE_REQUIREMENTS_FILE = "evidence_requirements.yaml"
CONFLICT_RULES_FILE = "conflict_rules.yaml"
STATUS = "pre-v1"  # every feature-side file in Milestone 8 (D§7); Milestone 12 freezes v1

# The eight features in their stable, explicit order (the FeatureVector's field order).
FEATURE_NAMES: tuple[str, ...] = (
    "irreversibility",
    "blast_radius",
    "persistence_risk",
    "scope_outside_target",
    "confidence_deficit",
    "evidence_insufficiency",
    "prerequisites_unverified",
    "telemetry_conflict",
)  # tests/test_m8_policy.py pins this to FeatureVector's fields

_RULE_ID = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


def _status(body: dict, name: str) -> None:
    if body["status"] != STATUS:
        raise PolicyError(f"{name}: status must be {STATUS!r}, got {body['status']!r}")


# --- config/gate_weights.yaml ----------------------------------------------------------------


@dataclass(frozen=True)
class GateWeights:
    gate_feature_version: int
    weights: Mapping[str, Fraction]  # FEATURE_NAMES order


def parse_gate_weights(loaded: PolicyFile) -> GateWeights:
    name = loaded.name
    body = require_keys(loaded.data, {"status", "gate_feature_version", "weights"}, name)
    _status(body, name)
    version = body["gate_feature_version"]
    if type(version) is not int or version < 0:
        raise PolicyError(f"{name}: gate_feature_version must be an integer >= 0, got {version!r}")
    raw = require_keys(body["weights"], set(FEATURE_NAMES), f"{name}: weights")
    weights = {f: decimal(raw[f], f"{name}: weights.{f}") for f in FEATURE_NAMES}
    if sum(weights.values()) != 1:
        raise PolicyError(f"{name}: weights sum to {sum(weights.values())}, not exactly 1")
    return GateWeights(version, MappingProxyType(weights))


# --- config/evidence_requirements.yaml -------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRequirements:
    requirements: Mapping[ActionType, tuple[str, ...]]
    prerequisites: frozenset[str]
    sha256: str


def _distinct_ids(value: object, where: str) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise PolicyError(f"{where}: expected a non-empty list of ids")
    if any(type(v) is not str for v in value):
        raise PolicyError(f"{where}: ids are strings")
    if len(set(value)) != len(value):
        raise PolicyError(f"{where}: duplicate id")
    return tuple(value)


def parse_evidence_requirements(loaded: PolicyFile) -> EvidenceRequirements:
    name = loaded.name
    body = require_keys(loaded.data, {"status", "requirements", "prerequisites"}, name)
    _status(body, name)
    raw = body["requirements"]
    if type(raw) is not dict:
        raise PolicyError(f"{name}: requirements must be a mapping")
    table: dict[ActionType, tuple[str, ...]] = {}
    for key, ids in raw.items():
        where = f"{name}: requirements.{key}"
        action = action_type(key, where)
        ids = _distinct_ids(ids, where)
        for rid in ids:
            owner = evidence.REQUIREMENTS.get(rid)
            if owner is None or owner[0] is not action:
                raise PolicyError(f"{where}: {rid!r} is not a requirement of {action.value}")
        table[action] = ids
    if set(table) != set(ActionType):
        missing = sorted(a.value for a in set(ActionType) - set(table))
        raise PolicyError(f"{name}: missing requirement sets for {missing}")
    prerequisites = _distinct_ids(body["prerequisites"], f"{name}: prerequisites")
    unknown = sorted(set(prerequisites) - set(evidence.PREREQUISITES))
    if unknown:
        raise PolicyError(f"{name}: unknown prerequisite ids {unknown}")
    return EvidenceRequirements(MappingProxyType(table), frozenset(prerequisites), loaded.sha256)


# --- config/conflict_rules.yaml ----------------------------------------------------------------


@dataclass(frozen=True)
class ConflictRules:
    rules: tuple[conflict.ConflictRule, ...]
    sha256: str


def _rule(raw: object, where: str) -> conflict.ConflictRule:
    raw = require_keys(raw, {"id", "tool", "field", "operator", "value"}, where)
    rid, tool, field, op, value = (raw[k] for k in ("id", "tool", "field", "operator", "value"))
    if type(rid) is not str or _RULE_ID.fullmatch(rid) is None:
        raise PolicyError(f"{where}: id {rid!r} must be a lowercase name")
    fields = conflict.FIELDS.get(tool) if type(tool) is str else None
    if fields is None:
        raise PolicyError(f"{where}: unknown tool {tool!r}")
    if type(field) is not str or field not in fields:
        raise PolicyError(f"{where}: {tool} has no scalar field {field!r}")
    if type(op) is not str or op not in conflict.OPERATORS:
        raise PolicyError(f"{where}: unknown operator {op!r}")
    if type(value) is not fields[field]:
        raise PolicyError(f"{where}: {field} compares with a {fields[field].__name__} literal")
    if op in conflict.ORDERING and fields[field] is not int:
        raise PolicyError(f"{where}: {op} applies to integer fields only")
    return conflict.ConflictRule(rid, tool, field, op, value)


def parse_conflict_rules(loaded: PolicyFile) -> ConflictRules:
    name = loaded.name
    body = require_keys(loaded.data, {"status", "rules"}, name)
    _status(body, name)
    if type(body["rules"]) is not list:
        raise PolicyError(f"{name}: rules must be a list")
    rules = tuple(_rule(r, f"{name}: rules[{i}]") for i, r in enumerate(body["rules"]))
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise PolicyError(f"{name}: duplicate rule id")
    return ConflictRules(rules, loaded.sha256)


# --- the bundle --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyBundle:
    """The loaded Milestone 8 policy condition, shared by extractor, scorer and writer."""

    action_semantics: ActionSemantics
    gate_weights: GateWeights
    evidence: EvidenceRequirements
    conflict: ConflictRules

    def provenance(self) -> dict:
        """The four policy-provenance values of D§7.1, from exactly the loaded bytes."""
        return {
            "action_semantics_hash": self.action_semantics.sha256,
            "evidence_requirements_hash": self.evidence.sha256,
            "conflict_rules_hash": self.conflict.sha256,
            "gate_feature_version": self.gate_weights.gate_feature_version,
        }


def load_policy(config_dir: Path = CONFIG_DIR) -> PolicyBundle:
    """Load the four files, each read exactly once."""
    config_dir = Path(config_dir)
    return PolicyBundle(
        action_semantics=parse_action_semantics(
            read_policy_file(config_dir / ACTION_SEMANTICS_FILE)
        ),
        gate_weights=parse_gate_weights(read_policy_file(config_dir / GATE_WEIGHTS_FILE)),
        evidence=parse_evidence_requirements(
            read_policy_file(config_dir / EVIDENCE_REQUIREMENTS_FILE)
        ),
        conflict=parse_conflict_rules(read_policy_file(config_dir / CONFLICT_RULES_FILE)),
    )
