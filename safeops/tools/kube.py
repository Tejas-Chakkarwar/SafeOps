"""The Kubernetes read tools (design §11.3; decision 0023): get_pods, get_events, get_logs,
get_deployment, get_rollout_history, get_configmap and get_service.

Each one validates its arguments before any read, reads through the safeops-reader client
only, projects the response onto its contract (never a raw object: no managedFields,
annotations, labels beyond a Service's selector, owner bookkeeping or Secret contents),
orders it deterministically, bounds it, and returns the JSON-safe result. A failed read
raises ToolError; it never becomes an empty result.

Cluster data is data (Rule 21): a log line or event message that reads like an instruction
is returned verbatim, as a string, and nothing here interprets it.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from pydantic import ValidationError

from safeops.tools import contracts as c
from safeops.tools.canonical import normalize
from safeops.tools.contracts import ToolError
from safeops.tools.reader import ToolContext, cap, items, parse_time

# Harness objects the agent must never see (Rule 1, §11.1 "explicitly ignored"): Chaos Mesh
# custom resources are the injection mechanism itself, and their events name it.
HARNESS_API_GROUPS = frozenset({"chaos-mesh.org"})
_HARNESS_COMPONENT = re.compile(r"chaos", re.I)


def _result(model: type, **fields) -> dict:
    """Build the contract model (which checks every count and flag against its evidence),
    then the JSON-safe form. A projection that can't satisfy its own contract is a response
    the tool can't represent honestly: malformed, never a partial success."""
    try:
        return normalize(model(**fields).model_dump(mode="json"))
    except ValidationError as exc:
        raise ToolError(
            "malformed_response", f"{model.__name__}: {exc.errors()[0]['msg']}"
        ) from exc


def _projected(fn):
    """Run a projection; a response missing what the contract needs is malformed."""
    try:
        return fn()
    except ToolError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ToolError("malformed_response", f"unexpected response shape: {exc!r}"[:300]) from exc


def _window(ctx: ToolContext, last_minutes: int):
    until = ctx.now()
    since = max(until - timedelta(minutes=last_minutes), ctx.not_before.astimezone(until.tzinfo))
    return since, until


# --- get_pods ---------------------------------------------------------------------------------


def _container_status(cs: dict) -> dict:
    state = cs.get("state") or {}
    kind = next((k for k in ("running", "waiting", "terminated") if k in state), "unknown")
    detail = state.get(kind) or {}
    last = (cs.get("lastState") or {}).get("terminated") or {}
    return {
        "name": cs["name"],
        "ready": bool(cs.get("ready", False)),
        "restart_count": int(cs.get("restartCount", 0)),
        "state": kind,
        "reason": cap(detail["reason"], c.MAX_REASON)[0]
        if kind in ("waiting", "terminated") and detail.get("reason")
        else None,
        "exit_code": int(detail["exitCode"]) if kind == "terminated" else None,
        "last_termination_reason": cap(last["reason"], c.MAX_REASON)[0]
        if last.get("reason") and "exitCode" in last
        else None,
        "last_exit_code": int(last["exitCode"]) if "exitCode" in last else None,
    }


def _pod(pod: dict) -> dict:
    meta, status = pod["metadata"], pod.get("status") or {}
    conditions = {x.get("type"): x.get("status") for x in status.get("conditions") or []}
    statuses = sorted(status.get("containerStatuses") or [], key=lambda x: x["name"])
    containers = [_container_status(x) for x in statuses]
    return {
        "name": meta["name"],
        "phase": status.get("phase") or "Unknown",
        "ready": conditions.get("Ready") == "True",
        "terminating": meta.get("deletionTimestamp") is not None,
        "restarts": sum(x["restart_count"] for x in containers),
        "started_at": parse_time(status["startTime"]) if status.get("startTime") else None,
        "containers": containers[: c.MAX_CONTAINERS],
        "container_count": len(containers),
    }


def get_pods(ctx: ToolContext, namespace: str) -> dict:
    """Pod status only (§11.3): phase, readiness, restarts and container states, by name."""
    args = c.parse_args(c.PodsArgs, {"namespace": namespace})
    ctx.require_namespace(args.namespace)
    listed = items(ctx.read_json("get", "pods", "-n", args.namespace))
    pods = _projected(lambda: sorted((_pod(p) for p in listed), key=lambda p: p["name"]))
    shown = pods[: c.MAX_PODS]
    return _result(
        c.PodsData,
        namespace=args.namespace,
        pods=shown,
        pod_count=len(shown),
        total_pods=len(pods),
        truncated=len(pods) > len(shown),
    )


# --- get_events -------------------------------------------------------------------------------


def is_harness_event(event: dict) -> bool:
    """An event about a harness object, or emitted by the fault backend."""
    involved = event.get("involvedObject") or {}
    api_version = str(involved.get("apiVersion") or "")
    group = api_version.split("/", 1)[0] if "/" in api_version else ""
    if group in HARNESS_API_GROUPS:
        return True
    emitters = (
        (event.get("source") or {}).get("component"),
        event.get("reportingController"),
        event.get("reportingComponent"),
    )
    return any(isinstance(e, str) and _HARNESS_COMPONENT.search(e) for e in emitters)


def _event(e: dict) -> dict:
    meta = e["metadata"]
    series = e.get("series") or {}
    last = (
        series.get("lastObservedTime")
        or e.get("lastTimestamp")
        or e.get("eventTime")
        or e.get("firstTimestamp")
        or meta["creationTimestamp"]
    )
    first = e.get("firstTimestamp") or e.get("eventTime")
    message, truncated = cap(str(e.get("message") or ""), c.MAX_TEXT)
    involved = e.get("involvedObject") or {}
    return {
        "last_seen": parse_time(last),
        "first_seen": parse_time(first) if first else None,
        "type": str(e.get("type") or "")[:32],
        "reason": str(e.get("reason") or "")[: c.MAX_REASON],
        "object": {
            "kind": str(involved.get("kind") or "")[:63],
            "name": str(involved.get("name") or "")[:253],
        },
        "message": message,
        "message_truncated": truncated,
        "count": max(1, int(series.get("count") or e.get("count") or 1)),
    }


def get_events(ctx: ToolContext, namespace: str, last_minutes: int) -> dict:
    """Events of the current incident, at most 50, oldest first. The 50 most recent matching
    events are kept; ties are broken on every listed field (decision 0023)."""
    args = c.parse_args(c.EventsArgs, {"namespace": namespace, "last_minutes": last_minutes})
    ctx.require_namespace(args.namespace)
    since, until = _window(ctx, args.last_minutes)
    listed = items(ctx.read_json("get", "events", "-n", args.namespace))

    def project():
        events = [c.Event(**_event(e)) for e in listed if not is_harness_event(e)]
        return sorted((e for e in events if e.last_seen >= since), key=c.event_order)

    matched = _projected(project)
    shown = matched[-c.MAX_EVENTS :]
    return _result(
        c.EventsData,
        namespace=args.namespace,
        last_minutes=args.last_minutes,
        since=since,
        until=until,
        events=shown,
        event_count=len(shown),
        matched_count=len(matched),
        truncated=len(matched) > len(shown),
    )


# --- get_logs ---------------------------------------------------------------------------------

_PG_LEVEL = re.compile(r"\b(DEBUG[1-5]?|INFO|NOTICE|LOG|WARNING|ERROR|FATAL|PANIC):")
_PG_MAP = {"INFO": "info", "NOTICE": "info", "LOG": "info", "WARNING": "warn"}
_JSON_LEVEL = {
    "debug": "debug",
    "info": "info",
    "warn": "warn",
    "warning": "warn",
    "error": "error",
    "fatal": "error",
    "critical": "error",
}


def line_level(text: str) -> str | None:
    """The severity a log line declares: the testbed's JSON `level`, or a Postgres prefix.
    None when it declares none (a traceback, say); such lines are kept at every severity,
    because dropping possible error evidence is worse than showing noise (decision 0023)."""
    if text.startswith("{"):
        try:
            level = json.loads(text).get("level")
        except (ValueError, AttributeError):
            level = None
        return _JSON_LEVEL.get(str(level).lower()) if level is not None else None
    match = _PG_LEVEL.search(text)
    if match is None:
        return None
    word = match.group(1)
    if word.startswith("DEBUG"):
        return "debug"
    return _PG_MAP.get(word, "error")  # ERROR, FATAL, PANIC


def _streams(pod: dict) -> list[tuple[str, bool]]:
    """(container, has a log stream) for each container, by name. A container that never
    started has nothing to read; that is not a failed read."""
    statuses = {s["name"]: s for s in (pod.get("status") or {}).get("containerStatuses") or []}
    out = []
    for spec in sorted(pod["spec"]["containers"], key=lambda x: x["name"]):
        status = statuses.get(spec["name"]) or {}
        state = status.get("state") or {}
        started = (
            "running" in state
            or "terminated" in state
            or "terminated" in (status.get("lastState") or {})
        )
        out.append((spec["name"], started))
    return out


def get_logs(
    ctx: ToolContext, service: str, last_minutes: int, severity: str, max_lines: int
) -> dict:
    """The newest `max_lines` lines (at most 200; more is rejected, never clamped) at or
    above `severity` from the service's pods, since the current incident began, oldest
    first. Each stream is read with a hard tail, so no read is unbounded."""
    args = c.parse_args(
        c.LogsArgs,
        {
            "service": service,
            "last_minutes": last_minutes,
            "severity": severity,
            "max_lines": max_lines,
        },
    )
    ctx.require_service(args.service)
    since, until = _window(ctx, args.last_minutes)
    ns = ctx.namespace
    listed = items(ctx.read_json("get", "pods", "-n", ns, "-l", f"app={args.service}"))
    pods = _projected(lambda: sorted(listed, key=lambda p: p["metadata"]["name"]))
    floor = c.SEVERITY_RANK[args.severity]
    since_arg = since.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    matched: list[tuple] = []
    unavailable: list[str] = []
    scan_limited = False
    for pod in pods[: c.MAX_LOG_PODS]:
        name = pod["metadata"]["name"]
        streams = _projected(lambda pod=pod: _streams(pod))
        scan_limited = scan_limited or len(streams) > c.MAX_LOG_CONTAINERS
        for container, started in streams[: c.MAX_LOG_CONTAINERS]:
            if not started:
                unavailable.append(f"{name}/{container}")
                continue
            out = ctx.read(
                "logs",
                name,
                "-n",
                ns,
                "-c",
                container,
                "--timestamps",
                f"--since-time={since_arg}",
                f"--tail={c.LOG_SCAN_LINES + 1}",
            )
            raw = out.splitlines()
            if len(raw) > c.LOG_SCAN_LINES:
                scan_limited, raw = True, raw[-c.LOG_SCAN_LINES :]
            for index, line in enumerate(raw):
                stamp, _, text = line.partition(" ")
                ts = _projected(lambda stamp=stamp: parse_time(stamp))
                if ts < since:
                    continue
                level = line_level(text)
                if level is not None and c.SEVERITY_RANK[level] < floor:
                    continue
                matched.append((ts, name, container, index, level, text))
    matched.sort(key=lambda m: m[:4])
    shown = matched[-args.max_lines :] if matched else []
    lines = []
    for ts, pod_name, container, _index, level, text in shown:
        text, truncated = cap(text, c.MAX_LOG_TEXT)
        lines.append(
            {
                "ts": ts,
                "pod": pod_name,
                "container": container,
                "level": level,
                "text": text,
                "text_truncated": truncated,
            }
        )
    return _result(
        c.LogsData,
        service=args.service,
        last_minutes=args.last_minutes,
        severity=args.severity,
        max_lines=args.max_lines,
        since=since,
        until=until,
        lines=lines,
        line_count=len(lines),
        matched_count=len(matched),
        truncated=len(matched) > len(lines),
        # Every stream that was not fully read, or not read at all, is admitted here.
        scan_limited=scan_limited
        or len(pods) > c.MAX_LOG_PODS
        or len(set(unavailable)) > c.MAX_UNAVAILABLE,
        unavailable=sorted(set(unavailable))[: c.MAX_UNAVAILABLE],
    )


# --- container specs (get_deployment, get_rollout_history) ----------------------------------


def _env_var(e: dict) -> dict:
    name = str(e["name"])
    ref = e.get("valueFrom")
    if ref is not None:
        source = next(
            (
                k
                for k in ("configMapKeyRef", "secretKeyRef", "fieldRef", "resourceFieldRef")
                if k in ref
            ),
            "other",
        )
        detail = ref.get(source) or {}
        value_from = {
            "source": source,
            "name": detail.get("name") or detail.get("fieldPath") or detail.get("resource"),
            "key": detail.get("key"),
        }
        return {
            "name": name,
            "value": None,
            "value_truncated": False,
            "redacted": False,
            "value_from": value_from,
        }
    if c.is_secret_key(name):
        return {
            "name": name,
            "value": None,
            "value_truncated": False,
            "redacted": True,
            "value_from": None,
        }
    value, truncated = cap(str(e.get("value", "")), c.MAX_TEXT)
    return {
        "name": name,
        "value": value,
        "value_truncated": truncated,
        "redacted": False,
        "value_from": None,
    }


def _env_from(e: dict) -> dict:
    source = next((k for k in ("configMapRef", "secretRef") if k in e), "other")
    return {"source": source, "name": (e.get(source) or {}).get("name"), "prefix": e.get("prefix")}


def _probe(p: dict | None) -> dict | None:
    if not p:
        return None
    handler = next((k for k in ("httpGet", "tcpSocket", "exec", "grpc") if k in p), "none")
    detail = p.get(handler) or {}
    command = detail.get("command") if handler == "exec" else None
    return {
        "handler": handler,
        "path": cap(str(detail["path"]), c.MAX_TEXT)[0] if detail.get("path") else None,
        "port": detail.get("port"),
        "command": [cap(str(x), c.MAX_TEXT)[0] for x in command[: c.MAX_COMMAND]]
        if command
        else None,
        "initial_delay_seconds": p.get("initialDelaySeconds"),
        "period_seconds": p.get("periodSeconds"),
        "timeout_seconds": p.get("timeoutSeconds"),
        "failure_threshold": p.get("failureThreshold"),
        "success_threshold": p.get("successThreshold"),
    }


def _quantities(d: dict | None) -> dict:
    return {str(k): str(v) for k, v in sorted((d or {}).items())}


def _container_spec(spec: dict) -> dict:
    env = spec.get("env") or []
    res = spec.get("resources") or {}
    return {
        "name": spec["name"],
        "image": str(spec.get("image", "")),
        "env": [_env_var(e) for e in env[: c.MAX_ENV]],
        "env_count": len(env),
        "env_from": [_env_from(e) for e in (spec.get("envFrom") or [])[: c.MAX_ENV_FROM]],
        "requests": _quantities(res.get("requests")),
        "limits": _quantities(res.get("limits")),
        "readiness_probe": _probe(spec.get("readinessProbe")),
        "liveness_probe": _probe(spec.get("livenessProbe")),
    }


def _containers(pod_spec: dict) -> tuple[list[dict], int]:
    specs = sorted(pod_spec["containers"], key=lambda x: x["name"])
    return [_container_spec(s) for s in specs[: c.MAX_CONTAINERS]], len(specs)


def _revision(meta: dict) -> int:
    value = (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision")
    if value is None or not str(value).isdigit() or int(value) < 1:
        raise ValueError(f"{meta.get('name')}: no valid deployment.kubernetes.io/revision")
    return int(value)


def _one(ctx: ToolContext, resource: str, args) -> dict | None:
    """The one named object, or None if it doesn't exist. A field-selected list, so a missing
    object is an answer from the API server, not an error to parse out of stderr."""
    listed = items(
        ctx.read_json(
            "get", resource, "-n", args.namespace, "--field-selector", f"metadata.name={args.name}"
        )
    )
    if len(listed) > 1:
        raise ToolError("malformed_response", f"{len(listed)} {resource} named {args.name!r}")
    return listed[0] if listed else None


def _named(model, namespace: str, name: str, ctx: ToolContext):
    args = c.parse_args(model, {"namespace": namespace, "name": name})
    ctx.require_namespace(args.namespace)
    return args


# --- get_deployment ---------------------------------------------------------------------------


def get_deployment(ctx: ToolContext, namespace: str, name: str) -> dict:
    """Spec and current revision (§11.3): replicas, and each container's image, env (secret
    values redacted), resources and probes."""
    args = _named(c.NamedArgs, namespace, name, ctx)
    obj = _one(ctx, "deployments", args)
    if obj is None:
        return _result(
            c.DeploymentData,
            namespace=args.namespace,
            name=args.name,
            found=False,
            current_revision=None,
            replicas=None,
            containers=None,
            container_count=None,
        )

    def project():
        meta = obj["metadata"]
        revision = (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision")
        containers, count = _containers(obj["spec"]["template"]["spec"])
        return {
            "current_revision": _revision(meta) if revision is not None else None,
            "replicas": int(obj["spec"].get("replicas", 1)),
            "containers": containers,
            "container_count": count,
        }

    return _result(
        c.DeploymentData,
        namespace=args.namespace,
        name=args.name,
        found=True,
        **_projected(project),
    )


# --- get_rollout_history -----------------------------------------------------------------------


def get_rollout_history(ctx: ToolContext, namespace: str, name: str) -> dict:
    """The last 5 revisions of a Deployment, from its ReplicaSets (§11.3), oldest first."""
    args = _named(c.NamedArgs, namespace, name, ctx)
    listed = items(ctx.read_json("get", "replicasets", "-n", args.namespace))

    def project():
        revisions = []
        for rs in listed:
            meta = rs["metadata"]
            owner = next(
                (o for o in meta.get("ownerReferences") or [] if o.get("controller")), None
            )
            if owner is None or owner.get("kind") != "Deployment" or owner.get("name") != args.name:
                continue
            containers, count = _containers(rs["spec"]["template"]["spec"])
            revisions.append(
                {
                    "revision": _revision(meta),
                    "replica_set": meta["name"],
                    "created_at": parse_time(meta["creationTimestamp"]),
                    "replicas": int(rs["spec"].get("replicas", 0)),
                    "ready_replicas": int((rs.get("status") or {}).get("readyReplicas", 0)),
                    "containers": containers,
                    "container_count": count,
                }
            )
        return sorted(revisions, key=lambda r: (r["revision"], r["replica_set"]))

    revisions = _projected(project)
    if len({r["revision"] for r in revisions}) != len(revisions):
        raise ToolError("malformed_response", "two ReplicaSets claim the same revision")
    shown = revisions[-c.MAX_REVISIONS :]
    return _result(
        c.RolloutHistoryData,
        namespace=args.namespace,
        name=args.name,
        revisions=shown,
        revision_count=len(shown),
        total_revisions=len(revisions),
        truncated=len(revisions) > len(shown),
    )


# --- get_configmap ------------------------------------------------------------------------------


def get_configmap(ctx: ToolContext, namespace: str, name: str) -> dict:
    """Content, secrets redacted (§11.3): a value whose key names a secret is withheld, length
    included; long values are cut at 4096 characters, flagged; binary keys are named only."""
    args = _named(c.NamedArgs, namespace, name, ctx)
    obj = _one(ctx, "configmaps", args)
    if obj is None:
        return _result(
            c.ConfigMapData,
            namespace=args.namespace,
            name=args.name,
            found=False,
            entries=[],
            entry_count=0,
            total_keys=0,
            truncated=False,
            binary_keys=[],
        )

    def project():
        data = obj.get("data") or {}
        entries = []
        for key in sorted(data):
            if c.is_secret_key(key):
                entries.append(
                    {
                        "key": key,
                        "value": None,
                        "length": None,
                        "value_truncated": False,
                        "redacted": True,
                    }
                )
                continue
            value = str(data[key])
            shown, truncated = cap(value, c.MAX_VALUE)
            entries.append(
                {
                    "key": key,
                    "value": shown,
                    "length": len(value),
                    "value_truncated": truncated,
                    "redacted": False,
                }
            )
        return entries, sorted(obj.get("binaryData") or {})

    entries, binary = _projected(project)
    shown = entries[: c.MAX_CONFIGMAP_KEYS]
    return _result(
        c.ConfigMapData,
        namespace=args.namespace,
        name=args.name,
        found=True,
        entries=shown,
        entry_count=len(shown),
        total_keys=len(entries),
        truncated=len(entries) > len(shown),
        binary_keys=binary[: c.MAX_CONFIGMAP_KEYS],
    )


# --- get_service -----------------------------------------------------------------------------


def endpoint_counts(slices: list[dict]) -> tuple[int, int]:
    """(ready, total) endpoints across a Service's EndpointSlices. Ready means
    conditions.ready is true, exactly as the canonical snapshot counts it (§11.1)."""
    ready = total = 0
    for sl in slices:
        for ep in sl.get("endpoints") or []:
            total += 1
            if (ep.get("conditions") or {}).get("ready") is True:
                ready += 1
    return ready, total


def get_service(ctx: ToolContext, namespace: str, name: str) -> dict:
    """Selector and ready endpoint count, read from EndpointSlices (§11.3)."""
    args = c.parse_args(c.NamedArgs, {"namespace": namespace, "name": name})
    ctx.require_namespace(args.namespace)
    obj = _one(ctx, "services", args)
    if obj is None:
        return _result(
            c.ServiceData,
            namespace=args.namespace,
            name=args.name,
            found=False,
            selector=None,
            ready_endpoint_count=None,
            endpoint_count=None,
        )
    slices = items(
        ctx.read_json(
            "get",
            "endpointslices.discovery.k8s.io",
            "-n",
            args.namespace,
            "-l",
            f"kubernetes.io/service-name={args.name}",
        )
    )
    selector = _projected(
        lambda: {str(k): str(v) for k, v in (obj["spec"].get("selector") or {}).items()}
    )
    ready, total = _projected(lambda: endpoint_counts(slices))
    return _result(
        c.ServiceData,
        namespace=args.namespace,
        name=args.name,
        found=True,
        selector=selector,
        ready_endpoint_count=ready,
        endpoint_count=total,
    )
