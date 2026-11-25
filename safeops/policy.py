"""Experiment-critical policy files: strict loading, exact bytes, and the frozen action
semantics (design §9.2, §10.8; decision 0025 D§7, D§7.1).

The one loading path for config/action_semantics.yaml, gate_weights.yaml,
evidence_requirements.yaml and conflict_rules.yaml:

- **Read once.** A file's bytes are read exactly once. Those bytes are hashed, and those same
  bytes are decoded and parsed. Nothing reopens the file to hash it, and nothing hashes a
  reserialized or normalized form: the provenance hash names exactly what was evaluated.
- **Strict.** Invalid UTF-8, malformed YAML, a duplicate mapping key at any depth, a merge
  key and a non-string key are refused. Last-key-wins would let an ambiguity change an
  experimental condition silently.
- **A configuration failure is a harness failure** (PolicyError), never an agent outcome.

The action-semantics table lives here because it is shared: the feature extractor reads it
(M8A) and the executor will (M8B), and neither may import the other. The feature-side files
(weights, evidence requirements, conflict rules) are loaded by safeops.features.policy,
through read_policy_file below.

Neutral (decision 0020): PyYAML, the standard library and safeops.enums only. Protected by
tests/test_information_wall.py, which also checks it never reaches safeops.cluster or
safeops.tools.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType

import yaml

from safeops.enums import ActionType

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
ACTION_SEMANTICS_FILE = "action_semantics.yaml"

# The bounded composite (§9.2): one SafeOps action, two Kubernetes writes, in this order.
# Every other action is exactly one write of its own type.
COMPOSITE_STEPS: Mapping[ActionType, tuple[ActionType, ...]] = MappingProxyType(
    {ActionType.PATCH_CONFIGMAP: (ActionType.PATCH_CONFIGMAP, ActionType.RESTART_DEPLOYMENT)}
)

_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")
_KIND = re.compile(r"[A-Z][A-Za-z0-9]{0,62}")


class PolicyError(ValueError):
    """A policy file that can't be loaded exactly as written: a harness failure."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys, merge keys and non-string keys."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise PolicyError(f"line {key_node.start_mark.line + 1}: merge keys are refused")
        key = loader.construct_object(key_node, deep=True)
        if type(key) is not str:
            raise PolicyError(f"line {key_node.start_mark.line + 1}: key {key!r} is not a string")
        if key in mapping:
            raise PolicyError(f"line {key_node.start_mark.line + 1}: duplicate key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass(frozen=True)
class PolicyFile:
    """One policy file as loaded: the sha256 of its exact bytes, and what those bytes parse to."""

    name: str
    sha256: str
    data: object


def sha256_of(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def read_policy_file(path: Path) -> PolicyFile:
    """Read `path` once; hash and parse exactly those bytes."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"{path.name}: cannot be read: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"{path.name}: not UTF-8: {exc}") from exc
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 (a SafeLoader subclass)
    except PolicyError as exc:
        raise PolicyError(f"{path.name}: {exc}") from None
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path.name}: not valid YAML: {exc}") from exc
    return PolicyFile(name=path.name, sha256=sha256_of(raw), data=data)


def require_keys(value: object, keys: set[str], where: str) -> dict:
    """`value` is a mapping with exactly `keys`."""
    if type(value) is not dict:
        raise PolicyError(f"{where}: expected a mapping, got {type(value).__name__}")
    if set(value) != keys:
        missing, unknown = sorted(keys - set(value)), sorted(set(value) - keys)
        raise PolicyError(f"{where}: missing {missing}, unknown {unknown}")
    return value


def decimal(value: object, where: str) -> Fraction:
    """A quoted decimal string ("0.125"), exactly. A YAML number is refused: its float
    conversion would already have rounded it."""
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:
        raise PolicyError(f"{where}: expected a quoted decimal string, got {value!r}")
    return Fraction(value)


def action_type(value: object, where: str) -> ActionType:
    if type(value) is not str or value not in ActionType._value2member_map_:
        raise PolicyError(f"{where}: unknown action type {value!r}")
    return ActionType(value)


# --- config/action_semantics.yaml (§9.2; not pre-v1) ---------------------------------------------


@dataclass(frozen=True)
class ActionSemantic:
    target_kind: str
    reversibility: Fraction
    steps: tuple[ActionType, ...]


@dataclass(frozen=True)
class ActionSemantics:
    actions: Mapping[ActionType, ActionSemantic]
    sha256: str

    def __getitem__(self, action: ActionType) -> ActionSemantic:
        return self.actions[action]


def expected_steps(action: ActionType) -> tuple[ActionType, ...]:
    return COMPOSITE_STEPS.get(action, (action,))


def parse_action_semantics(loaded: PolicyFile) -> ActionSemantics:
    body = require_keys(loaded.data, {"actions"}, loaded.name)
    raw = body["actions"]
    if type(raw) is not dict:
        raise PolicyError(f"{loaded.name}: actions must be a mapping")
    table: dict[ActionType, ActionSemantic] = {}
    for name, entry in raw.items():
        where = f"{loaded.name}: {name}"
        action = action_type(name, where)
        entry = require_keys(entry, {"target_kind", "reversibility", "steps"}, where)
        kind = entry["target_kind"]
        if type(kind) is not str or _KIND.fullmatch(kind) is None:
            raise PolicyError(f"{where}: target_kind {kind!r} is not a Kubernetes kind")
        reversibility = decimal(entry["reversibility"], f"{where}.reversibility")
        if not 0 <= reversibility <= 1:
            raise PolicyError(f"{where}: reversibility {entry['reversibility']} is outside [0, 1]")
        steps = entry["steps"]
        if type(steps) is not list:
            raise PolicyError(f"{where}: steps must be a list")
        steps = tuple(action_type(s, f"{where}.steps") for s in steps)
        if steps != expected_steps(action):
            raise PolicyError(
                f"{where}: steps must be {[s.value for s in expected_steps(action)]} (§9.2)"
            )
        table[action] = ActionSemantic(kind, reversibility, steps)
    if set(table) != set(ActionType):
        missing = sorted(a.value for a in set(ActionType) - set(table))
        raise PolicyError(f"{loaded.name}: missing actions {missing}")
    return ActionSemantics(actions=MappingProxyType(table), sha256=loaded.sha256)


def load_action_semantics(path: Path | None = None) -> ActionSemantics:
    return parse_action_semantics(read_policy_file(path or CONFIG_DIR / ACTION_SEMANTICS_FILE))
