"""Canonical semantic snapshots of the testbed (design §11.1; decisions 0011, 0013).

Hand-written on purpose (Rule 16): a bug here produces wrong harm numbers without
crashing. Snapshots are semantic projections, never raw objects. Everything Kubernetes
churns on its own (status, resourceVersion, generation, managedFields, timestamps, UIDs,
annotations, controller-made ReplicaSets and Pods) is dropped, and API-server defaults are
normalized, so a manifest and the live object it produced project identically.

Projected fields (§11.1):
  Deployment   replicas; per container: image, env (incl. ConfigMap/Secret refs), envFrom,
               readiness, liveness and startup probes (every Probe field), resource
               requests and limits (kept separate,
               decision 0011); config volumes
  Service      selector, ports, headless, type, ready_endpoint_count,
               endpoint_owner_services (from EndpointSlices)
  ConfigMap    sha256 of the approved keys, plus the parsed value of every numeric approved key
  StatefulSet  replicas, serviceName, containers (as above), volumeClaimTemplates
  PVC          bound or not

Pure functions only: no cluster access and no I/O, so tests drive them with fabricated objects.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from fractions import Fraction

# One definition, owned by safeops.topology: the ownership map, the snapshot and clean-state
# verification must all mean the same set of kinds (decision 0020). Re-exported here.
from safeops.topology import PROJECTED_KINDS  # noqa: F401

# Created by Kubernetes in every namespace; not part of the testbed (same rule as ownership).
IGNORED = frozenset({("ConfigMap", "kube-root-ca.crt")})
SNAPSHOT_PROJECTION_VERSION = 2  # 2: full Probe projection + startupProbe (0013)


# --- quantities --------------------------------------------------------------------------

_BIN = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
_DEC = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "m": Fraction(1, 1000)}


def _quantity(q: str | int | float) -> Fraction:
    s = str(q).strip()
    for suffix, mult in _BIN.items():
        if s.endswith(suffix):
            return Fraction(s[: -len(suffix)]) * mult
    for suffix, mult in _DEC.items():
        if s.endswith(suffix):
            return Fraction(s[: -len(suffix)]) * mult
    return Fraction(s)


def cpu_millis(q) -> float:
    v = _quantity(q) * 1000
    return int(v) if v.denominator == 1 else float(v)


def memory_mib(q) -> float:
    v = _quantity(q) / 2**20
    return int(v) if v.denominator == 1 else round(float(v), 6)


def _resources(res: dict | None) -> dict:
    out: dict = {}
    for section in ("requests", "limits"):  # kept separate, never collapsed (decision 0011)
        vals = (res or {}).get(section) or {}
        out[section] = {
            "cpu_millis": cpu_millis(vals["cpu"]) if "cpu" in vals else None,
            "memory_mib": memory_mib(vals["memory"]) if "memory" in vals else None,
        }
    return out


# --- probes and env ----------------------------------------------------------------------

_PROBE_DEFAULTS = {
    "initialDelaySeconds": 0,
    "periodSeconds": 10,
    "timeoutSeconds": 1,
    "successThreshold": 1,
    "failureThreshold": 3,
    "terminationGracePeriodSeconds": None,  # probe-level override; None = pod's own grace period
}


def _probe(p: dict | None) -> dict | None:
    """Every behavior-relevant field of a Kubernetes v1 Probe (audited at the Milestone 3
    third review, against core/v1 Probe and ProbeHandler):

      handlers  exec.command; httpGet.{path, port, host, scheme, httpHeaders};
                tcpSocket.{port, host}; grpc.{port, service}
      timing    initialDelaySeconds, periodSeconds, timeoutSeconds, successThreshold,
                failureThreshold, terminationGracePeriodSeconds

    No field of Probe is excluded. Defaults are applied, so a manifest and the live object
    project identically. HTTP header names are lowercased, because header names are
    case-insensitive in HTTP, and headers are sorted by (name, value); values are kept
    exactly. Repeated headers are kept, since repetition is meaningful.
    """
    if not p:
        return None
    out: dict = {k: p.get(k, v) for k, v in _PROBE_DEFAULTS.items()}
    if "httpGet" in p:
        h = p["httpGet"]
        headers = sorted(
            (
                {"name": str(x.get("name", "")).lower(), "value": x.get("value", "")}
                for x in h.get("httpHeaders") or []
            ),
            key=lambda x: (x["name"], x["value"]),
        )
        out["httpGet"] = {
            "path": h.get("path", "/"),
            "port": h.get("port"),
            "scheme": h.get("scheme", "HTTP"),
            "host": h.get("host"),
            "httpHeaders": headers,
        }
    if "exec" in p:
        out["exec"] = {"command": list(p["exec"].get("command") or [])}
    if "tcpSocket" in p:
        out["tcpSocket"] = {"port": p["tcpSocket"].get("port"), "host": p["tcpSocket"].get("host")}
    if "grpc" in p:
        out["grpc"] = {"port": p["grpc"].get("port"), "service": p["grpc"].get("service", "")}
    return out


def _env(env: list | None) -> list:
    out = []
    for e in env or []:
        item: dict = {"name": e["name"]}
        if "valueFrom" in e:
            vf = e["valueFrom"]
            for ref in ("configMapKeyRef", "secretKeyRef"):
                if ref in vf:
                    item[ref] = {
                        "name": vf[ref].get("name"),
                        "key": vf[ref].get("key"),
                        "optional": bool(vf[ref].get("optional", False)),
                    }
            if "fieldRef" in vf:
                item["fieldRef"] = vf["fieldRef"].get("fieldPath")
            if "resourceFieldRef" in vf:
                item["resourceFieldRef"] = vf["resourceFieldRef"].get("resource")
        else:
            item["value"] = e.get("value", "")
        out.append(item)  # order kept: it is semantic for $(VAR) expansion
    return out


def _env_from(env_from: list | None) -> list:
    out = []
    for e in env_from or []:
        for ref in ("configMapRef", "secretRef"):
            if ref in e:
                out.append({ref: e[ref].get("name"), "prefix": e.get("prefix", "")})
    return out


def _containers(spec: dict) -> list:
    out = []
    for c in sorted(spec.get("containers") or [], key=lambda c: c["name"]):
        out.append(
            {
                "name": c["name"],
                "image": c.get("image"),
                "env": _env(c.get("env")),
                "envFrom": _env_from(c.get("envFrom")),
                "readinessProbe": _probe(c.get("readinessProbe")),
                "livenessProbe": _probe(c.get("livenessProbe")),
                # Not in §11.1's list, but it gates readiness and can restart the
                # container, so it's projected too (decision 0013, third review).
                "startupProbe": _probe(c.get("startupProbe")),
                "resources": _resources(c.get("resources")),
            }
        )
    return out


def _config_volumes(spec: dict) -> list:
    out = []
    for v in spec.get("volumes") or []:
        for ref, key in (
            ("configMap", "name"),
            ("secret", "secretName"),
            ("persistentVolumeClaim", "claimName"),
        ):
            if ref in v:
                out.append({"volume": v["name"], ref: v[ref].get(key)})
    return sorted(out, key=lambda x: x["volume"])


# --- per kind ----------------------------------------------------------------------------


def deployment(obj: dict) -> dict:
    spec = obj.get("spec", {})
    pod = spec.get("template", {}).get("spec", {})
    return {
        "replicas": spec.get("replicas", 1),
        "containers": _containers(pod),
        "volumes": _config_volumes(pod),
    }


def statefulset(obj: dict) -> dict:
    spec = obj.get("spec", {})
    pod = spec.get("template", {}).get("spec", {})
    vcts = []
    for t in spec.get("volumeClaimTemplates") or []:
        ts = t.get("spec", {})
        storage = ((ts.get("resources") or {}).get("requests") or {}).get("storage")
        vcts.append(
            {
                "name": t["metadata"]["name"],
                "accessModes": sorted(ts.get("accessModes") or []),
                "storage_mib": memory_mib(storage) if storage else None,
            }
        )
    return {
        "replicas": spec.get("replicas", 1),
        "serviceName": spec.get("serviceName"),
        "containers": _containers(pod),
        "volumes": _config_volumes(pod),
        "volumeClaimTemplates": sorted(vcts, key=lambda v: v["name"]),
    }


def configmap(obj: dict, approved_keys: dict | None) -> dict:
    data = obj.get("data") or {}
    if not approved_keys:
        # Not a topology ConfigMap: hash everything, so any change is visible.
        blob = json.dumps(data, sort_keys=True).encode()
        return {"data_sha256": hashlib.sha256(blob).hexdigest()}
    present = {k: data[k] for k in sorted(approved_keys) if k in data}
    numeric: dict = {}
    for key, rule in sorted(approved_keys.items()):
        if "hard_range" in rule:
            raw = data.get(key)
            try:
                value = float(raw) if raw is not None else None
            except ValueError:
                value = f"unparseable:{raw}"
            if isinstance(value, float) and not math.isfinite(value):
                # NaN != NaN would make identical states diff (review L1); keep it as text.
                value = f"non-finite:{raw}"
            numeric[key] = value
    return {
        "approved_keys_sha256": hashlib.sha256(
            json.dumps(present, sort_keys=True).encode()
        ).hexdigest(),
        "missing_approved_keys": sorted(set(approved_keys) - set(data)),
        "numeric": numeric,
    }


def service(obj: dict, ready_endpoint_count: int, endpoint_owner_services: list[str]) -> dict:
    spec = obj.get("spec", {})
    ports = [
        {
            "name": p.get("name", ""),
            "port": p.get("port"),
            "targetPort": p.get("targetPort", p.get("port")),
            "protocol": p.get("protocol", "TCP"),
        }
        for p in spec.get("ports") or []
    ]
    return {
        "selector": dict(sorted((spec.get("selector") or {}).items())),
        "ports": sorted(ports, key=lambda p: (p["name"], p["port"])),
        "type": spec.get("type", "ClusterIP"),
        "headless": spec.get("clusterIP") == "None",
        "ready_endpoint_count": ready_endpoint_count,
        "endpoint_owner_services": sorted(endpoint_owner_services),
    }


def pvc(obj: dict) -> dict:
    return {"bound": obj.get("status", {}).get("phase") == "Bound"}


# --- endpoints -------------------------------------------------------------------------


def pod_owners(pods: list[dict], replicasets: list[dict], owner_of) -> dict[str, str]:
    """pod name -> owning topology service, via ownerReferences (Pod -> RS -> Deployment,
    or Pod -> StatefulSet). Not via labels, which a rogue pod could copy."""
    rs_owner = {}
    for rs in replicasets:
        for ref in rs["metadata"].get("ownerReferences") or []:
            if ref.get("controller"):
                rs_owner[rs["metadata"]["name"]] = (ref["kind"], ref["name"])
    out = {}
    for pod in pods:
        name = pod["metadata"]["name"]
        ctrl = next(
            (r for r in pod["metadata"].get("ownerReferences") or [] if r.get("controller")), None
        )
        workload = None
        if ctrl is not None and ctrl["kind"] == "ReplicaSet":
            workload = rs_owner.get(ctrl["name"])
        elif ctrl is not None:
            workload = (ctrl["kind"], ctrl["name"])
        out[name] = owner_of(*workload) if workload else f"unowned:Pod/{name}"
    return out


def endpoints_for(
    service_name: str, slices: list[dict], owners: dict[str, str]
) -> tuple[int, list[str]]:
    ready, owner_set = 0, set()
    for sl in slices:
        if (sl["metadata"].get("labels") or {}).get("kubernetes.io/service-name") != service_name:
            continue
        for ep in sl.get("endpoints") or []:
            if (ep.get("conditions") or {}).get("ready") is not True:
                continue
            ready += 1
            ref = ep.get("targetRef") or {}
            owner_set.add(
                owners.get(ref.get("name"), f"unknown:{ref.get('kind')}/{ref.get('name')}")
            )
    return ready, sorted(owner_set)


# --- snapshot ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """{(kind, name): canonical state}. Compare with diff(); identity via digest().

    Two serializations, deliberately separate (decision 0020):
    - to_json() is the canonical resource form that digest() hashes. It is unchanged, so
      every recorded snapshot hash and desired_state_hash keeps its meaning.
    - to_persisted_json() / from_persisted_json() are for storage. They carry the
      projection version, and loading refuses any other version, or none: old snapshot
      data is never silently stamped with the current version.
    """

    resources: dict = field(default_factory=dict)
    projection_version: int = SNAPSHOT_PROJECTION_VERSION

    def _canonical(self) -> dict:
        return {f"{k}/{n}": v for (k, n), v in sorted(self.resources.items())}

    def to_json(self) -> str:
        return json.dumps(self._canonical(), sort_keys=True, indent=2)

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_json().encode()).hexdigest()

    def to_persisted_json(self) -> str:
        return json.dumps(
            {"projection_version": self.projection_version, "resources": self._canonical()},
            sort_keys=True,
            indent=2,
        )

    @classmethod
    def from_persisted_json(cls, text: str) -> Snapshot:
        raw = json.loads(text)
        if not isinstance(raw, dict) or set(raw) != {"projection_version", "resources"}:
            raise ValueError(
                "not a persisted snapshot: expected {projection_version, resources}"
                " (an unversioned snapshot is refused, never assigned the current version)"
            )
        version = raw["projection_version"]
        if version != SNAPSHOT_PROJECTION_VERSION or isinstance(version, bool):
            raise ValueError(
                f"snapshot projection version {version!r} is not the current"
                f" {SNAPSHOT_PROJECTION_VERSION}; re-capture it instead of reinterpreting it"
            )
        return cls(
            {tuple(key.split("/", 1)): value for key, value in raw["resources"].items()},
            projection_version=version,
        )


def build(
    objects: list[dict], slices: list[dict], pods: list[dict], replicasets: list[dict], topology
) -> Snapshot:
    """Project live (or rendered) testbed objects into a Snapshot."""

    def owner_of(kind: str, name: str) -> str:
        spec = topology.resource(kind, name)
        return spec.owner if spec is not None else f"unmapped:{kind}/{name}"

    owners = pod_owners(pods, replicasets, owner_of)
    out: dict = {}
    for obj in objects:
        kind, name = obj["kind"], obj["metadata"]["name"]
        if kind not in PROJECTED_KINDS or (kind, name) in IGNORED:
            continue
        if kind == "Deployment":
            state = deployment(obj)
        elif kind == "StatefulSet":
            state = statefulset(obj)
        elif kind == "ConfigMap":
            spec = topology.resource("ConfigMap", name)
            state = configmap(obj, spec.approved_keys if spec else None)
        elif kind == "Service":
            state = service(obj, *endpoints_for(name, slices, owners))
        else:
            state = pvc(obj)
        out[(kind, name)] = state
    return Snapshot(out)


def diff(expected: Snapshot, actual: Snapshot) -> list[str]:
    """Human-readable differences. Empty exactly when the digests are equal (decision 0020):
    the comparator and the canonical hash mean the same identity."""
    if expected.projection_version != actual.projection_version:
        raise ValueError(
            f"can't compare projection versions {expected.projection_version} and"
            f" {actual.projection_version}"
        )
    problems = []
    for key in sorted(set(expected.resources) | set(actual.resources)):
        label = f"{key[0]}/{key[1]}"
        if key not in actual.resources:
            problems.append(f"{label}: missing")
        elif key not in expected.resources:
            problems.append(f"{label}: unexpected")
        else:
            problems.extend(_diff_values(label, expected.resources[key], actual.resources[key]))
    return problems


def _diff_values(path: str, a, b) -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            # Key membership is explicit: a key present with None is not a missing key.
            if k not in b:
                out.append(f"{path}.{k}: missing (expected {a[k]!r})")
            elif k not in a:
                out.append(f"{path}.{k}: unexpected (got {b[k]!r})")
            else:
                out.extend(_diff_values(f"{path}.{k}", a[k], b[k]))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out.extend(_diff_values(f"{path}[{i}]", x, y))
        return out
    # Leaves compare the way the digest sees them: as canonical JSON. Python's == would
    # call 1, 1.0 and True equal although their JSON, and so the digest, differs.
    return [] if _canon(a) == _canon(b) else [f"{path}: expected {a!r}, got {b!r}"]


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True)
