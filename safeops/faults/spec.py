"""Fault specifications (design §9.5): `faults/<id>.yaml`, parsed strictly.

A FaultSpec is evaluator ground truth. Only its ``symptoms`` reach the alert generator,
and they reach it inside a FaultVerification, never as part of the spec (decision 0015).

Import policy (decision 0004): reachable from RUN_SEQUENCE, so PyYAML and the topology
loader are imported inside functions, never at module scope.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from pathlib import Path

from safeops.experiment.unit import MAX_FAULT_SEED, validate_fault_id

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FAULTS_DIR = REPO_ROOT / "faults"

# §9.1's frozen action names. The ActionType enum itself arrives at Milestone 8; until
# then a remediation oracle is checked against these names so a typo can't hide in one.
ACTION_TYPES = frozenset(
    {
        "rollback_deployment",
        "restart_deployment",
        "scale_deployment",
        "patch_configmap",
        "patch_service",
        "patch_deployment_resources",
    }
)

# Symptom metrics the harness can evaluate on its own measurement path (decision 0015).
SUPPORTED_METRICS = frozenset({"http_5xx_rate"})
# "<metric> > <threshold> over <N>s": a ratio over one fixed window, not a sustained
# "for" condition (decision 0015 says why the design's "for 2m" isn't used).
_EXPRESSION = re.compile(
    r"(?P<metric>[a-z][a-z0-9_]*) > (?P<threshold>0\.[0-9]+) over (?P<window>[1-9][0-9]*)s",
    re.ASCII,
)
_ALERT_NAME = re.compile(r"[A-Z][A-Za-z0-9]{0,63}", re.ASCII)
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}", re.ASCII)
_K8S_NAME = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", re.ASCII)
_REMEDIATION = re.compile(
    r"(?P<action>[a-z_]+):(?P<target>[a-z0-9-]+)(?::(?P<cond>\S+))?", re.ASCII
)

_TOP_KEYS = {
    "id",
    "affected_service",
    "symptoms",
    "injection",
    "valid_remediations",
    "harmful_remediations",
    "self_healing_possible",
    "safe_autonomous_remediation_exists",
}
_SYMPTOM_KEYS = {"alert_name", "target_service", "expression"}
_INJECTION_KEYS = {"backend", "action", "target", "variants"}
_TARGET_KEYS = {"kind", "name", "container"}
# The only injection mechanism implemented so far. New ones arrive with their faults.
_MECHANISMS = {("kubernetes-api", "set_container_env")}
MAX_SYMPTOM_WINDOW_S = 300


class FaultSpecError(ValueError):
    """A fault spec is missing, unreadable, or doesn't match the schema."""


@dataclass(frozen=True)
class SymptomExpression:
    metric: str
    threshold: float
    window_s: int


@dataclass(frozen=True)
class Symptom:
    """One observable symptom, as declared under `symptoms:`. The only spec content that
    may cross into the agent's view (as an Alert)."""

    alert_name: str
    target_service: str
    expression: str

    @property
    def parsed(self) -> SymptomExpression:
        return parse_expression(self.expression)


@dataclass(frozen=True)
class Variant:
    env: str
    value: str


@dataclass(frozen=True)
class Injection:
    backend: str
    action: str
    kind: str
    name: str
    container: str
    variants: tuple[Variant, ...]


@dataclass(frozen=True)
class FaultSpec:
    id: str
    affected_service: str
    symptoms: tuple[Symptom, ...]
    injection: Injection
    valid_remediations: tuple[str, ...]
    harmful_remediations: tuple[str, ...]
    self_healing_possible: bool
    safe_autonomous_remediation_exists: bool
    definition_sha256: str  # of the file's bytes; fault_definition_hash in §11.8

    def variant_for(self, fault_seed: int) -> tuple[int, Variant]:
        """The variant ``fault_seed`` selects: same seed, same injected fault (§10.1).

        Uses ``random.Random(seed).random()``, the one output CPython guarantees to stay
        the same across versions for the same seed (decision 0014).
        """
        if (
            not isinstance(fault_seed, int)
            or isinstance(fault_seed, bool)
            or not 0 <= fault_seed <= MAX_FAULT_SEED
        ):
            raise FaultSpecError(f"fault_seed must be an integer in 0..{MAX_FAULT_SEED}")
        n = len(self.injection.variants)
        index = min(int(random.Random(fault_seed).random() * n), n - 1)
        return index, self.injection.variants[index]


def parse_expression(text: str) -> SymptomExpression:
    m = _EXPRESSION.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise FaultSpecError(f"symptom expression {text!r} must read '<metric> > <0.x> over <N>s'")
    metric, threshold, window_s = m["metric"], float(m["threshold"]), int(m["window"])
    if metric not in SUPPORTED_METRICS:
        raise FaultSpecError(f"symptom metric {metric!r} is not one the harness can measure")
    if not 0 < threshold < 1:
        raise FaultSpecError(f"symptom threshold {threshold} must be strictly between 0 and 1")
    if window_s > MAX_SYMPTOM_WINDOW_S:
        raise FaultSpecError(f"symptom window {window_s}s exceeds {MAX_SYMPTOM_WINDOW_S}s")
    return SymptomExpression(metric, threshold, window_s)


def _keys(where: str, obj: object, expected: set[str]) -> dict:
    if not isinstance(obj, dict):
        raise FaultSpecError(f"{where} must be a mapping")
    if set(obj) != expected:
        missing, extra = expected - set(obj), set(obj) - expected
        raise FaultSpecError(f"{where}: missing {sorted(missing)}, unexpected {sorted(extra)}")
    return obj


def _str(where: str, value: object, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value or (pattern and not pattern.fullmatch(value)):
        raise FaultSpecError(f"{where} is not valid: {value!r}")
    return value


def _remediations(where: str, value: object, services: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise FaultSpecError(f"{where} must be a list")
    for item in value:
        m = _REMEDIATION.fullmatch(item) if isinstance(item, str) else None
        if m is None or m["action"] not in ACTION_TYPES or m["target"] not in services:
            raise FaultSpecError(f"{where}: {item!r} is not '<action_type>:<service>[:cond]'")
    return tuple(value)


def parse_fault_spec(raw: bytes, fault_id: str, topology) -> FaultSpec:
    import yaml

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FaultSpecError(f"faults/{fault_id}.yaml is not valid YAML: {exc}") from exc
    data = _keys(f"faults/{fault_id}.yaml", data, _TOP_KEYS)
    if data["id"] != fault_id:
        raise FaultSpecError(f"faults/{fault_id}.yaml declares id {data['id']!r}")
    services = topology.services

    affected = _str("affected_service", data["affected_service"])
    if affected not in services:
        raise FaultSpecError(f"affected_service {affected!r} is not in the topology")

    raw_symptoms = data["symptoms"]
    if not isinstance(raw_symptoms, list) or len(raw_symptoms) != 1:
        # One symptom, one alert, until a fault genuinely needs more (decision 0015).
        raise FaultSpecError("symptoms must list exactly one symptom")
    symptoms = []
    for i, s in enumerate(raw_symptoms):
        s = _keys(f"symptoms[{i}]", s, _SYMPTOM_KEYS)
        target = _str(f"symptoms[{i}].target_service", s["target_service"])
        if target not in services:
            raise FaultSpecError(f"symptoms[{i}].target_service {target!r} is not in the topology")
        symptom = Symptom(
            _str(f"symptoms[{i}].alert_name", s["alert_name"], _ALERT_NAME),
            target,
            _str(f"symptoms[{i}].expression", s["expression"]),
        )
        parse_expression(symptom.expression)
        symptoms.append(symptom)

    inj = _keys("injection", data["injection"], _INJECTION_KEYS)
    if (inj["backend"], inj["action"]) not in _MECHANISMS:
        raise FaultSpecError(f"injection {inj['backend']}/{inj['action']} is not implemented")
    target = _keys("injection.target", inj["target"], _TARGET_KEYS)
    if target["kind"] != "Deployment":
        raise FaultSpecError("set_container_env targets a Deployment")
    name = _str("injection.target.name", target["name"], _K8S_NAME)
    resource = topology.resources.get(name)
    if resource is None or resource.kind != "Deployment":
        raise FaultSpecError(f"injection target Deployment/{name} is not in the topology")
    container = _str("injection.target.container", target["container"], _K8S_NAME)
    variants = inj["variants"]
    if not isinstance(variants, list) or not variants:
        raise FaultSpecError("injection.variants must be a non-empty list")
    parsed_variants = []
    for i, v in enumerate(variants):
        v = _keys(f"injection.variants[{i}]", v, {"env", "value"})
        if not isinstance(v["value"], str):
            raise FaultSpecError(f"injection.variants[{i}].value must be a string")
        parsed_variants.append(
            Variant(_str(f"injection.variants[{i}].env", v["env"], _ENV_NAME), v["value"])
        )
    if len(set(parsed_variants)) != len(parsed_variants):
        raise FaultSpecError("injection.variants has duplicates")

    for key in ("self_healing_possible", "safe_autonomous_remediation_exists"):
        if not isinstance(data[key], bool):
            raise FaultSpecError(f"{key} must be true or false")

    return FaultSpec(
        id=fault_id,
        affected_service=affected,
        symptoms=tuple(symptoms),
        injection=Injection(
            inj["backend"], inj["action"], "Deployment", name, container, tuple(parsed_variants)
        ),
        valid_remediations=_remediations(
            "valid_remediations", data["valid_remediations"], services
        ),
        harmful_remediations=_remediations(
            "harmful_remediations", data["harmful_remediations"], services
        ),
        self_healing_possible=data["self_healing_possible"],
        safe_autonomous_remediation_exists=data["safe_autonomous_remediation_exists"],
        definition_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def fault_path(fault_id: str, faults_dir: Path = FAULTS_DIR) -> Path:
    """Where ``fault_id``'s spec lives. The id is validated before it touches a path, and
    the result must sit directly inside ``faults_dir`` (no symlink out of it either)."""
    validate_fault_id(fault_id)
    root = Path(faults_dir).resolve()
    path = (root / f"{fault_id}.yaml").resolve()
    if path.parent != root:
        raise FaultSpecError(f"fault {fault_id!r} resolves outside {root}")
    return path


def load_fault_spec(fault_id: str, faults_dir: Path = FAULTS_DIR, topology=None) -> FaultSpec:
    path = fault_path(fault_id, faults_dir)
    if not path.is_file():
        raise FaultSpecError(f"no fault spec for {fault_id!r} (expected faults/{fault_id}.yaml)")
    if topology is None:
        from safeops.topology import load_topology

        topology = load_topology()
    return parse_fault_spec(path.read_bytes(), fault_id, topology)
