"""Service topology and resource ownership (design §12; decision 0007).

The single implementation of the graph closures. Both features/ (scope_outside_target,
blast_radius) and verify/ (the action-policy harm rule) import it, so the two cannot
drift apart (decision 0004 review notes). The policy built on top stays separate: the
feature produces a number, the harm checker a label, and neither calls the other.

Graph direction: depends_on edges point from caller to callee.
    dependencies(s): services s calls, transitively  -> incident_scope
    dependents(s):   services that call s, transitively -> blast_radius_set
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY_PATH = REPO_ROOT / "config" / "topology.yaml"

# Kinds the canonical snapshot projects (design §11.1). The single definition (decision
# 0020): safeops.environment.snapshot, clean-state verification and bootstrap all import
# this one. The ownership map must resolve every testbed object of these kinds.
PROJECTED_KINDS = ("Deployment", "Service", "ConfigMap", "StatefulSet", "PersistentVolumeClaim")
RANGED_FIELDS = ("replicas", "memory_mib", "cpu_millis")


class TopologyError(ValueError):
    pass


@dataclass(frozen=True)
class Range:
    lo: float
    hi: float

    def contains(self, x: float) -> bool:
        return self.lo <= x <= self.hi


@dataclass(frozen=True)
class Bounds:
    hard: Range
    safe: Range


@dataclass(frozen=True)
class ResourceSpec:
    name: str
    kind: str
    owner: str
    bounds: dict[str, Bounds] = field(default_factory=dict)  # replicas, memory_mib, ...
    approved_keys: dict[str, dict] = field(default_factory=dict)  # ConfigMaps only
    persistence_affecting: bool = False


@dataclass(frozen=True)
class ServiceTopology:
    depends_on: dict[str, tuple[str, ...]]
    stateful: frozenset[str]
    resources: dict[str, ResourceSpec]

    @property
    def services(self) -> tuple[str, ...]:
        return tuple(self.depends_on)

    def dependencies(self, service: str) -> set[str]:
        """Services `service` calls, transitively (follows depends_on)."""
        return self._closure(service, self.depends_on)

    def dependents(self, service: str) -> set[str]:
        """Services that call `service`, transitively (follows reverse edges)."""
        reverse: dict[str, list[str]] = {s: [] for s in self.depends_on}  # a local copy
        for caller, callees in self.depends_on.items():
            for callee in callees:
                reverse[callee].append(caller)
        return self._closure(service, reverse)

    def incident_scope(self, alert_target: str) -> set[str]:
        return {alert_target} | self.dependencies(alert_target)

    def blast_radius_set(self, target: str) -> set[str]:
        return {target} | self.dependents(target)

    def resource(self, kind: str, name: str) -> ResourceSpec | None:
        spec = self.resources.get(name)
        return spec if spec is not None and spec.kind == kind else None

    def owner_of(self, kind: str, name: str) -> str:
        spec = self.resource(kind, name)
        if spec is None:
            raise TopologyError(f"{kind}/{name} is not in the ownership map")
        return spec.owner

    def _closure(self, start: str, edges: dict) -> set[str]:
        if start not in self.depends_on:
            raise TopologyError(f"unknown service {start!r}")
        seen: set[str] = set()
        todo = list(edges[start])
        while todo:
            s = todo.pop()
            if s not in seen:
                seen.add(s)
                todo.extend(edges[s])
        return seen


def _mapping(raw: object, where: str) -> dict:
    """`raw` must be a mapping: checked before any .get(), `in` or iteration on it."""
    if type(raw) is not dict:
        raise TopologyError(f"{where}: expected a mapping, got {type(raw).__name__}")
    return raw


def _optional_mapping(parent: dict, key: str, where: str) -> dict:
    """An optional mapping field, in three states: absent is the empty default; present and
    a mapping (`{}` included) is that mapping; present and anything else ([], null, false, 0,
    "") is a TopologyError. Never `parent.get(key) or {}`: that lets a malformed value pass
    for an omitted one (decision 0025, M8A-2 strict-loader amendment)."""
    if key not in parent:
        return {}
    return _mapping(parent[key], where)


def _flag(raw: dict, key: str, where: str) -> bool:
    """An optional boolean: absent is false; present must be a real YAML boolean. A quoted
    "false" or a 0/1 is refused, never read through truthiness."""
    value = raw.get(key, False)
    if type(value) is not bool:
        raise TopologyError(f"{where}.{key}: expected true or false, got {value!r}")
    return value


def _endpoint(value: object) -> bool:
    """A finite int or float, never a bool (bool subclasses int) or NaN or an infinity."""
    return type(value) in (int, float) and math.isfinite(value)


def _range(raw: object, where: str) -> Range:
    if not (type(raw) is list and len(raw) == 2 and all(_endpoint(v) for v in raw)):
        raise TopologyError(f"{where}: expected [lo, hi] of finite numbers, got {raw!r}")
    lo, hi = raw
    if lo > hi:
        raise TopologyError(f"{where}: lo {lo} > hi {hi}")
    return Range(lo, hi)


def _bounds(raw: object, where: str) -> Bounds:
    raw = _mapping(raw, where)
    hard = _range(raw.get("hard_range"), f"{where}.hard_range")
    safe = _range(raw.get("safe_range"), f"{where}.safe_range")
    if not (hard.lo <= safe.lo and safe.hi <= hard.hi):
        raise TopologyError(f"{where}: safe_range must sit inside hard_range")
    return Bounds(hard, safe)


def _frozen(value):
    """Read-only deep copy: mappings become MappingProxyType, lists become tuples.

    The topology is shared by IncidentRuntimeContext and the harm checker. Nothing in the
    runtime may be able to change the scope rule the harm checker applies (Milestone 4
    review, finding 8)."""
    if isinstance(value, dict):
        return MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_frozen(v) for v in value)
    return value


def parse_topology(data: object) -> ServiceTopology:
    """The ServiceTopology of a parsed topology document. Every malformed shape is a
    TopologyError: bodies are checked as mappings before they're read, booleans are real
    booleans, and range endpoints are finite non-boolean numbers."""
    data = _mapping(data, "topology")
    services = _optional_mapping(data, "services", "services")
    depends_on: dict[str, tuple[str, ...]] = {}
    stateful: set[str] = set()
    for name, spec in services.items():
        spec = _mapping(spec, f"service {name}")
        callees = spec.get("depends_on", [])
        if type(callees) is not list or any(type(c) is not str for c in callees):
            raise TopologyError(f"service {name}: depends_on must be a list of service names")
        depends_on[name] = tuple(callees)
        if _flag(spec, "stateful", f"service {name}"):
            stateful.add(name)
    for name, callees in depends_on.items():
        for callee in callees:
            if callee not in depends_on:
                raise TopologyError(f"service {name} depends on unknown service {callee}")

    resources: dict[str, ResourceSpec] = {}
    for name, raw in _optional_mapping(data, "resources", "resources").items():
        raw = _mapping(raw, f"resource {name}")
        kind, owner = raw.get("kind"), raw.get("owner")
        if type(kind) is not str or kind not in PROJECTED_KINDS:
            raise TopologyError(f"resource {name}: kind {kind!r} is not a projected kind")
        if type(owner) is not str or owner not in depends_on:
            raise TopologyError(f"resource {name}: owner {owner!r} is not a service")
        bounds = {f: _bounds(raw[f], f"{name}.{f}") for f in RANGED_FIELDS if f in raw}
        if kind == "Deployment" and set(bounds) != set(RANGED_FIELDS):
            raise TopologyError(f"Deployment {name} needs bounds for {', '.join(RANGED_FIELDS)}")
        approved = _optional_mapping(raw, "approved_keys", f"resource {name}.approved_keys")
        # Unchanged: only non-empty approved_keys are ConfigMap-only; an explicit `{}` on
        # another kind declares no keys and stays accepted (M8A-2 strict-loader amendment).
        if approved and kind != "ConfigMap":
            raise TopologyError(f"resource {name}: approved_keys only apply to ConfigMaps")
        for key, rule in approved.items():
            where = f"{name}.approved_keys.{key}"
            rule = _mapping(rule, where)
            if "enum" in rule:
                if type(rule["enum"]) is not list or not rule["enum"]:
                    raise TopologyError(f"{where}.enum: expected a non-empty list")
            else:
                _bounds(rule, where)
        resources[name] = ResourceSpec(
            name=name,
            kind=kind,
            owner=owner,
            bounds=MappingProxyType(bounds),
            approved_keys=_frozen(dict(approved)),
            persistence_affecting=_flag(raw, "persistence_affecting", f"resource {name}"),
        )
    return ServiceTopology(
        depends_on=MappingProxyType(depends_on),
        stateful=frozenset(stateful),
        resources=MappingProxyType(resources),
    )


# Objects Kubernetes creates in every namespace on its own. Not part of the testbed.
_AUTO_CREATED = frozenset({("ConfigMap", "kube-root-ca.crt")})


def ownership_report(topology: ServiceTopology, items: list[dict]) -> list[str]:
    """Problems resolving live testbed objects against the ownership map, in both directions.

    `items` are live objects of PROJECTED_KINDS from the testbed namespace. Only objects
    Kubernetes creates in every namespace on its own (kube-root-ca.crt) are skipped. An
    object with ownerReferences is *not* skipped: §11.1 ignores controller-created
    ReplicaSets and Pods, which aren't projected kinds anyway, and a projected-kind object
    carrying an owner reference is still an application resource that must be accounted
    for. Skipping it would let a rogue Service or Deployment hide (Milestone 2 review, B2).
    An empty list means every object resolves to an entry and every entry exists.
    """
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for obj in items:
        kind, meta = obj["kind"], obj.get("metadata", {})
        name = meta["name"]
        if kind not in PROJECTED_KINDS:
            continue
        if (kind, name) in _AUTO_CREATED:
            continue
        seen.add((kind, name))
        if topology.resource(kind, name) is None:
            problems.append(f"{kind}/{name} is in the cluster but has no topology entry")
    for spec in topology.resources.values():
        if (spec.kind, spec.name) not in seen:
            problems.append(f"{spec.kind}/{spec.name} is declared but not in the cluster")
    return problems


@dataclass(frozen=True)
class TopologyCondition:
    """A parsed topology bound to the SHA-256 of the exact bytes it was parsed from
    (decision 0025, M8A-2 topology amendment). Whatever records `sha256` as topology_hash
    and uses `topology` can't be describing two different files."""

    topology: ServiceTopology
    sha256: str  # "sha256:" + 64 lowercase hex, over the file's bytes as read


def load_topology_condition(path: Path | None = None) -> TopologyCondition:
    """Read the topology file once; hash and parse exactly those bytes, strictly.

    The strict loader is safeops.policy's (duplicate, merge and non-string keys refused,
    strict UTF-8). Its failures are translated back to TopologyError: a bad topology stays
    a topology error, never a policy one."""
    from safeops.policy import PolicyError, read_policy_file

    try:
        loaded = read_policy_file(TOPOLOGY_PATH if path is None else path)
    except PolicyError as exc:
        raise TopologyError(str(exc)) from None
    try:
        topology = parse_topology(loaded.data)
    except TopologyError as exc:
        raise TopologyError(f"{loaded.name}: {exc}") from None
    return TopologyCondition(topology=topology, sha256=loaded.sha256)


def load_topology(path: Path | None = None) -> ServiceTopology:
    """The topology alone. Its consumers that also record topology_hash must use
    load_topology_condition instead, so both come from one read."""
    return load_topology_condition(path).topology
