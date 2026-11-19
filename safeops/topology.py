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

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY_PATH = REPO_ROOT / "config" / "topology.yaml"

# Kinds the canonical snapshot projects (design §11.1). The ownership map must resolve
# every testbed object of these kinds, and nothing else.
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
        reverse: dict[str, list[str]] = {s: [] for s in self.depends_on}
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


def _range(raw: object, where: str) -> Range:
    if not (
        isinstance(raw, list) and len(raw) == 2 and all(isinstance(v, int | float) for v in raw)
    ):
        raise TopologyError(f"{where}: expected [lo, hi], got {raw!r}")
    lo, hi = raw
    if lo > hi:
        raise TopologyError(f"{where}: lo {lo} > hi {hi}")
    return Range(lo, hi)


def _bounds(raw: dict, where: str) -> Bounds:
    hard = _range(raw.get("hard_range"), f"{where}.hard_range")
    safe = _range(raw.get("safe_range"), f"{where}.safe_range")
    if not (hard.lo <= safe.lo and safe.hi <= hard.hi):
        raise TopologyError(f"{where}: safe_range must sit inside hard_range")
    return Bounds(hard, safe)


def parse_topology(data: dict) -> ServiceTopology:
    services = data.get("services") or {}
    depends_on = {
        name: tuple((spec or {}).get("depends_on", [])) for name, spec in services.items()
    }
    for name, callees in depends_on.items():
        for callee in callees:
            if callee not in depends_on:
                raise TopologyError(f"service {name} depends on unknown service {callee}")
    stateful = frozenset(n for n, s in services.items() if (s or {}).get("stateful"))

    resources: dict[str, ResourceSpec] = {}
    for name, raw in (data.get("resources") or {}).items():
        kind, owner = raw.get("kind"), raw.get("owner")
        if kind not in PROJECTED_KINDS:
            raise TopologyError(f"resource {name}: kind {kind!r} is not a projected kind")
        if owner not in depends_on:
            raise TopologyError(f"resource {name}: owner {owner!r} is not a service")
        bounds = {f: _bounds(raw[f], f"{name}.{f}") for f in RANGED_FIELDS if f in raw}
        if kind == "Deployment" and set(bounds) != set(RANGED_FIELDS):
            raise TopologyError(f"Deployment {name} needs bounds for {', '.join(RANGED_FIELDS)}")
        approved = raw.get("approved_keys") or {}
        if approved and kind != "ConfigMap":
            raise TopologyError(f"resource {name}: approved_keys only apply to ConfigMaps")
        for key, rule in approved.items():
            if "enum" not in rule:
                _bounds(rule, f"{name}.approved_keys.{key}")
        resources[name] = ResourceSpec(
            name=name,
            kind=kind,
            owner=owner,
            bounds=bounds,
            approved_keys=dict(approved),
            persistence_affecting=bool(raw.get("persistence_affecting", False)),
        )
    return ServiceTopology(depends_on=depends_on, stateful=stateful, resources=resources)


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


def load_topology(path: Path = TOPOLOGY_PATH) -> ServiceTopology:
    import yaml

    return parse_topology(yaml.safe_load(path.read_text()))
