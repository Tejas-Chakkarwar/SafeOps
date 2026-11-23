"""The eight tools' contracts (design §11.3; decision 0023): arguments, bounded results, the
trajectory envelope, and the checks a stored step must pass.

One definition, used twice: the tools build their results through these models, and the
run-record schema (version 2) re-validates every stored step against them. A derived
field (a count, a truncation flag, an aggregate, the digest) must agree with the evidence
stored beside it (CLAUDE.md, the evidence-consistency invariant).

Arguments come from the model, so they are untrusted: strict types, closed enums, exact
ranges, Kubernetes name grammars. No argument is ever passed through as a kubectl flag, a
PromQL fragment, a path or a selector expression. Out-of-range input is rejected before any
read, never clamped: `max_lines` above 200 is an error, not 200.

Neutral: Pydantic and safeops.domain only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from safeops.domain import NonNegInt
from safeops.domain import StrictModel as _Strict
from safeops.tools.canonical import canonical_json, result_digest

# --- bounds (design §11.3; the ones the design leaves open are decision 0023's) ------------

MAX_EVENTS = 50  # §11.3
MAX_LOG_LINES = 200  # §11.3, hard cap
MAX_METRIC_POINTS = 60  # §11.3
MAX_REVISIONS = 5  # §11.3
MAX_PODS = 50
MAX_CONTAINERS = 10
MAX_ENV = 100
MAX_ENV_FROM = 20
MAX_COMMAND = 10
MAX_CONFIGMAP_KEYS = 64
MAX_SELECTOR = 32
MAX_MINUTES = 60  # last_minutes and window; Prometheus keeps 2 h (decision 0010)
MAX_TEXT = 1024  # one event message, one env value, one probe command element
MAX_LOG_TEXT = 2048  # one log line
MAX_VALUE = 4096  # one ConfigMap value
MAX_REASON = 128
MAX_ERROR = 500
MAX_UNAVAILABLE = 20
METRIC_MIN_STEP_S = 5  # the scrape interval (decision 0010)
METRIC_MIN_RANGE_S = 20  # four scrapes: rate() needs at least two samples
LOG_SCAN_LINES = 1000  # newest lines read per container stream before filtering
MAX_LOG_PODS = 20
MAX_LOG_CONTAINERS = 5

# --- argument grammars -----------------------------------------------------------------------

_LABEL = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
DnsLabel = Annotated[str, Field(pattern=f"^{_LABEL}$", max_length=63)]
DnsSubdomain = Annotated[str, Field(pattern=rf"^{_LABEL}(\.{_LABEL})*$", max_length=253)]
Minutes = Annotated[int, Field(ge=1, le=MAX_MINUTES)]
MaxLines = Annotated[int, Field(ge=1, le=MAX_LOG_LINES)]
Severity = Literal["debug", "info", "warn", "error"]
SEVERITY_RANK = {"debug": 10, "info": 20, "warn": 30, "error": 40}
Metric = Literal[
    "request_rate",
    "error_rate",
    "latency_p95_seconds",
    "cpu_usage_cores",
    "memory_working_set_bytes",
]
METRIC_UNITS: dict[str, str] = {
    "request_rate": "requests_per_second",
    "error_rate": "ratio",
    "latency_p95_seconds": "seconds",
    "cpu_usage_cores": "cores",
    "memory_working_set_bytes": "bytes",
}
ErrorKind = Literal[
    "invalid_argument",  # rejected before any read
    "credential",  # the reader credential is missing, expired or refused
    "guard",  # the client-side guard refused the call
    "command_failed",  # kubectl or the API server (or Prometheus through it) failed
    "timeout",
    "malformed_response",  # the response did not have the shape the contract needs
]

# A value is redacted when its key (or env var) names a secret (§11.3 "secrets redacted").
# Decided by the key alone, so a stored record's redactions can be re-derived exactly.
SECRET_KEY = re.compile(
    r"password|passwd|secret|token|credential|api[-_]?key|private[-_]?key", re.I
)


def is_secret_key(key: str) -> bool:
    return SECRET_KEY.search(key) is not None


class PodsArgs(_Strict):
    namespace: DnsLabel


class EventsArgs(_Strict):
    namespace: DnsLabel
    last_minutes: Minutes


class LogsArgs(_Strict):
    service: DnsLabel
    last_minutes: Minutes
    severity: Severity
    max_lines: MaxLines


class MetricsArgs(_Strict):
    service: DnsLabel
    metric: Metric
    window: Minutes


class NamedArgs(_Strict):
    namespace: DnsLabel
    name: DnsSubdomain


# --- shared result pieces ----------------------------------------------------------------------

Text = Annotated[str, Field(max_length=MAX_TEXT)]
Reason = Annotated[str, Field(max_length=MAX_REASON)]


def _truncation(
    p: list[str], what: str, shown: int, listed: int, total: int, truncated: bool, cap: int
) -> None:
    """count == len(list); total >= count; truncated exactly when total > count; a truncated
    list is full."""
    if shown != listed:
        p.append(f"{what}: count {shown} != {listed} listed")
    if total < listed:
        p.append(f"{what}: total {total} < {listed} listed")
    if truncated != (total > listed):
        p.append(f"{what}: truncated is {truncated} but {listed} of {total} are listed")
    if truncated and listed != cap:
        p.append(f"{what}: truncated, yet {listed} listed under a cap of {cap}")


def _capped_text(p: list[str], what: str, text: str | None, truncated: bool, cap: int) -> None:
    if text is None:
        if truncated:
            p.append(f"{what}: no text, yet truncated")
        return
    if truncated and len(text) != cap:
        p.append(f"{what}: truncated at {len(text)} characters, the cap is {cap}")
    if len(text) > cap:
        p.append(f"{what}: {len(text)} characters exceed the cap of {cap}")


def _fail(model: str, p: list[str]) -> None:
    if p:
        raise ValueError(f"{model}: " + "; ".join(p))


# --- get_pods -------------------------------------------------------------------------------------


class ContainerStatus(_Strict):
    name: DnsLabel
    ready: bool
    restart_count: NonNegInt
    state: Literal["running", "waiting", "terminated", "unknown"]
    reason: Reason | None
    exit_code: int | None
    last_termination_reason: Reason | None
    last_exit_code: int | None

    @model_validator(mode="after")
    def _shape(self) -> ContainerStatus:
        p = []
        if self.state in ("running", "unknown") and (self.reason, self.exit_code) != (None, None):
            p.append(f"a {self.state} container has no reason or exit code")
        if self.state == "waiting" and self.exit_code is not None:
            p.append("a waiting container has no exit code")
        if self.state == "terminated" and self.exit_code is None:
            p.append("a terminated container has an exit code")
        if self.last_exit_code is None and self.last_termination_reason is not None:
            p.append("a last termination reason without its exit code")
        _fail("container", p)
        return self


class PodStatus(_Strict):
    name: DnsSubdomain
    phase: Literal["Pending", "Running", "Succeeded", "Failed", "Unknown"]
    ready: bool
    terminating: bool
    restarts: NonNegInt
    started_at: AwareDatetime | None
    containers: Annotated[list[ContainerStatus], Field(max_length=MAX_CONTAINERS)]
    container_count: NonNegInt

    @model_validator(mode="after")
    def _counts(self) -> PodStatus:
        p: list[str] = []
        listed = len(self.containers)
        if listed != min(self.container_count, MAX_CONTAINERS):
            p.append(f"{listed} containers listed of {self.container_count}")
        names = [c.name for c in self.containers]
        if names != sorted(set(names)):
            p.append("containers must be sorted by name, without duplicates")
        if listed == self.container_count:
            total = sum(c.restart_count for c in self.containers)
            if self.restarts != total:
                p.append(f"restarts {self.restarts} != the containers' {total}")
        _fail(f"pod {self.name}", p)
        return self


class PodsData(_Strict):
    namespace: DnsLabel
    pods: Annotated[list[PodStatus], Field(max_length=MAX_PODS)]
    pod_count: NonNegInt
    total_pods: NonNegInt
    truncated: bool

    @model_validator(mode="after")
    def _counts(self) -> PodsData:
        p: list[str] = []
        _truncation(
            p, "pods", self.pod_count, len(self.pods), self.total_pods, self.truncated, MAX_PODS
        )
        names = [x.name for x in self.pods]
        if names != sorted(set(names)):
            p.append("pods must be sorted by name, without duplicates")
        _fail("get_pods", p)
        return self


# --- get_events -----------------------------------------------------------------------------------


class EventObject(_Strict):
    kind: Annotated[str, Field(max_length=63)]
    name: Annotated[str, Field(max_length=253)]


class Event(_Strict):
    last_seen: AwareDatetime
    first_seen: AwareDatetime | None
    type: Annotated[str, Field(max_length=32)]
    reason: Reason
    object: EventObject
    message: Text
    message_truncated: bool
    count: Annotated[int, Field(ge=1)]


def event_order(e) -> tuple:
    """The stable semantic order events are bounded and listed in: oldest first, ties broken
    on every listed field, so equal timestamps never depend on API ordering."""
    return (e.last_seen, e.object.kind, e.object.name, e.reason, e.type, e.message, e.count)


class EventsData(_Strict):
    namespace: DnsLabel
    last_minutes: Minutes
    since: AwareDatetime
    until: AwareDatetime
    events: Annotated[list[Event], Field(max_length=MAX_EVENTS)]
    event_count: NonNegInt
    matched_count: NonNegInt
    truncated: bool

    @model_validator(mode="after")
    def _evidence(self) -> EventsData:
        p: list[str] = []
        _truncation(
            p,
            "events",
            self.event_count,
            len(self.events),
            self.matched_count,
            self.truncated,
            MAX_EVENTS,
        )
        keys = [event_order(e) for e in self.events]
        if keys != sorted(keys):
            p.append("events must be listed oldest first, in the stable order")
        if self.since < self.until - timedelta(minutes=self.last_minutes):
            p.append("since is earlier than last_minutes allows")
        for i, e in enumerate(self.events):
            if e.last_seen < self.since:
                p.append(f"event {i} was last seen before since")
            _capped_text(p, f"event {i} message", e.message, e.message_truncated, MAX_TEXT)
        _fail("get_events", p)
        return self


# --- get_logs -------------------------------------------------------------------------------------


class LogLine(_Strict):
    ts: AwareDatetime
    pod: DnsSubdomain
    container: DnsLabel
    level: Severity | None  # None: the line carries no recognizable level
    text: Annotated[str, Field(max_length=MAX_LOG_TEXT)]
    text_truncated: bool


class LogsData(_Strict):
    service: DnsLabel
    last_minutes: Minutes
    severity: Severity
    max_lines: MaxLines
    since: AwareDatetime
    until: AwareDatetime
    lines: Annotated[list[LogLine], Field(max_length=MAX_LOG_LINES)]
    line_count: NonNegInt
    matched_count: NonNegInt
    truncated: bool
    # Not everything was read: a stream had more than LOG_SCAN_LINES lines in the window, or
    # some streams were skipped (more than MAX_LOG_PODS pods, more than MAX_LOG_CONTAINERS
    # containers in a pod, more than MAX_UNAVAILABLE unavailable streams to name).
    scan_limited: bool
    # Streams with nothing to read (a container that never started), as "pod/container".
    unavailable: Annotated[
        list[Annotated[str, Field(max_length=320)]], Field(max_length=MAX_UNAVAILABLE)
    ]

    @model_validator(mode="after")
    def _evidence(self) -> LogsData:
        p: list[str] = []
        listed = len(self.lines)
        if self.line_count != listed:
            p.append(f"line_count {self.line_count} != {listed} lines")
        if listed != min(self.matched_count, self.max_lines):
            p.append(
                f"{listed} lines listed; min(matched {self.matched_count}, max_lines) expected"
            )
        if self.truncated != (self.matched_count > self.max_lines):
            p.append(f"truncated is {self.truncated} for {self.matched_count} matched lines")
        keys = [(x.ts, x.pod, x.container) for x in self.lines]
        if keys != sorted(keys):
            p.append("lines must be listed oldest first")
        if self.since < self.until - timedelta(minutes=self.last_minutes):
            p.append("since is earlier than last_minutes allows")
        floor = SEVERITY_RANK[self.severity]
        for i, line in enumerate(self.lines):
            if line.ts < self.since:
                p.append(f"line {i} is older than since")
            if line.level is not None and SEVERITY_RANK[line.level] < floor:
                p.append(f"line {i} is {line.level}, below the requested {self.severity}")
            _capped_text(p, f"line {i}", line.text, line.text_truncated, MAX_LOG_TEXT)
        if self.unavailable != sorted(set(self.unavailable)):
            p.append("unavailable streams must be sorted, without duplicates")
        _fail("get_logs", p)
        return self


# --- get_metrics ----------------------------------------------------------------------------------


class NonFinite(_Strict):
    """The canonical non-finite tag (safeops.tools.canonical)."""

    non_finite: Literal["NaN", "+Inf", "-Inf"]


class MetricPoint(_Strict):
    t: AwareDatetime
    v: float | NonFinite


class MetricsData(_Strict):
    service: DnsLabel
    metric: Metric
    window: Minutes
    unit: str
    start: AwareDatetime
    end: AwareDatetime
    step_s: Annotated[int, Field(ge=METRIC_MIN_STEP_S)]
    range_s: Annotated[int, Field(ge=METRIC_MIN_RANGE_S)]
    points: Annotated[list[MetricPoint], Field(max_length=MAX_METRIC_POINTS)]
    point_count: NonNegInt
    truncated: bool

    @model_validator(mode="after")
    def _evidence(self) -> MetricsData:
        p: list[str] = []
        if self.unit != METRIC_UNITS[self.metric]:
            p.append(f"unit {self.unit!r} is not {METRIC_UNITS[self.metric]!r} for {self.metric}")
        if self.point_count != len(self.points):
            p.append(f"point_count {self.point_count} != {len(self.points)} points")
        if self.truncated and len(self.points) != MAX_METRIC_POINTS:
            p.append("truncated, yet the points are not at the cap")
        if self.range_s < self.step_s:
            p.append("range_s is shorter than step_s: samples between points would be skipped")
        if self.start < self.end - timedelta(minutes=self.window):
            p.append("start is earlier than the window allows")
        if self.start.microsecond or self.end.microsecond:
            p.append("start and end are whole seconds")
        span = (self.end - self.start).total_seconds()
        if span < 0:
            if self.points:
                p.append("an empty window (start after end) has no points")
        elif int(span) // self.step_s + 1 > MAX_METRIC_POINTS:
            p.append("start, end and step_s allow more than 60 points")
        times = [x.t for x in self.points]
        if times != sorted(set(times)):
            p.append("points must be strictly increasing in time")
        for x in self.points:
            offset = (x.t - self.start).total_seconds()
            if x.t > self.end or offset < 0 or offset % self.step_s:
                p.append(f"point {x.t.isoformat()} is not on the start + k * step_s grid")
                break
        _fail("get_metrics", p)
        return self


# --- get_deployment and get_rollout_history ----------------------------------------------------


class EnvRef(_Strict):
    source: Literal["configMapKeyRef", "secretKeyRef", "fieldRef", "resourceFieldRef", "other"]
    name: Annotated[str, Field(max_length=253)] | None
    key: Annotated[str, Field(max_length=253)] | None


class EnvVar(_Strict):
    name: Annotated[str, Field(max_length=253)]
    value: Text | None
    value_truncated: bool
    redacted: bool
    value_from: EnvRef | None

    @model_validator(mode="after")
    def _shape(self) -> EnvVar:
        p: list[str] = []
        if self.redacted != (is_secret_key(self.name) and self.value_from is None):
            p.append("a literal value is redacted exactly when its name names a secret")
        if self.redacted and (self.value is not None or self.value_truncated):
            p.append("a redacted value is not shown")
        if self.value_from is not None and self.value is not None:
            p.append("a referenced value has no literal value")
        _capped_text(p, f"env {self.name}", self.value, self.value_truncated, MAX_TEXT)
        _fail("env", p)
        return self


class EnvFrom(_Strict):
    source: Literal["configMapRef", "secretRef", "other"]
    name: Annotated[str, Field(max_length=253)] | None
    prefix: Annotated[str, Field(max_length=253)] | None


class Probe(_Strict):
    handler: Literal["httpGet", "tcpSocket", "exec", "grpc", "none"]
    path: Text | None
    port: int | Annotated[str, Field(max_length=63)] | None
    command: Annotated[list[Text], Field(max_length=MAX_COMMAND)] | None
    initial_delay_seconds: NonNegInt | None
    period_seconds: NonNegInt | None
    timeout_seconds: NonNegInt | None
    failure_threshold: NonNegInt | None
    success_threshold: NonNegInt | None


class ContainerSpec(_Strict):
    name: DnsLabel
    image: Annotated[str, Field(max_length=512)]
    env: Annotated[list[EnvVar], Field(max_length=MAX_ENV)]
    env_count: NonNegInt
    env_from: Annotated[list[EnvFrom], Field(max_length=MAX_ENV_FROM)]
    requests: dict[Annotated[str, Field(max_length=63)], Annotated[str, Field(max_length=63)]]
    limits: dict[Annotated[str, Field(max_length=63)], Annotated[str, Field(max_length=63)]]
    readiness_probe: Probe | None
    liveness_probe: Probe | None

    @model_validator(mode="after")
    def _counts(self) -> ContainerSpec:
        p: list[str] = []
        if len(self.env) != min(self.env_count, MAX_ENV):
            p.append(f"{len(self.env)} env vars listed of {self.env_count}")
        if len(self.requests) > 8 or len(self.limits) > 8:
            p.append("too many resource entries")
        _fail(f"container {self.name}", p)
        return self


def _containers_problems(p: list[str], containers, count: int) -> None:
    if len(containers) != min(count, MAX_CONTAINERS):
        p.append(f"{len(containers)} containers listed of {count}")
    names = [c.name for c in containers]
    if names != sorted(set(names)):
        p.append("containers must be sorted by name, without duplicates")


class DeploymentData(_Strict):
    namespace: DnsLabel
    name: DnsSubdomain
    found: bool
    current_revision: Annotated[int, Field(ge=1)] | None
    replicas: NonNegInt | None
    containers: Annotated[list[ContainerSpec], Field(max_length=MAX_CONTAINERS)] | None
    container_count: NonNegInt | None

    @model_validator(mode="after")
    def _evidence(self) -> DeploymentData:
        p: list[str] = []
        spec = (self.current_revision, self.replicas, self.containers, self.container_count)
        if not self.found and spec != (None, None, None, None):
            p.append("a deployment that was not found has no spec")
        if self.found:
            if self.replicas is None or self.containers is None or self.container_count is None:
                p.append("a found deployment has replicas and containers")
            else:
                _containers_problems(p, self.containers, self.container_count)
        _fail("get_deployment", p)
        return self


class Revision(_Strict):
    revision: Annotated[int, Field(ge=1)]
    replica_set: DnsSubdomain
    created_at: AwareDatetime
    replicas: NonNegInt
    ready_replicas: NonNegInt
    containers: Annotated[list[ContainerSpec], Field(max_length=MAX_CONTAINERS)]
    container_count: NonNegInt

    @model_validator(mode="after")
    def _evidence(self) -> Revision:
        p: list[str] = []
        _containers_problems(p, self.containers, self.container_count)
        _fail(f"revision {self.revision}", p)
        return self


class RolloutHistoryData(_Strict):
    namespace: DnsLabel
    name: DnsSubdomain
    revisions: Annotated[list[Revision], Field(max_length=MAX_REVISIONS)]
    revision_count: NonNegInt
    total_revisions: NonNegInt
    truncated: bool

    @model_validator(mode="after")
    def _evidence(self) -> RolloutHistoryData:
        p: list[str] = []
        _truncation(
            p,
            "revisions",
            self.revision_count,
            len(self.revisions),
            self.total_revisions,
            self.truncated,
            MAX_REVISIONS,
        )
        numbers = [r.revision for r in self.revisions]
        if numbers != sorted(set(numbers)):
            p.append("revisions must be strictly increasing (the newest last)")
        _fail("get_rollout_history", p)
        return self


# --- get_configmap -------------------------------------------------------------------------------


class ConfigEntry(_Strict):
    key: Annotated[str, Field(max_length=253)]
    value: Annotated[str, Field(max_length=MAX_VALUE)] | None
    length: NonNegInt | None  # characters in the stored value; withheld when redacted
    value_truncated: bool
    redacted: bool

    @model_validator(mode="after")
    def _evidence(self) -> ConfigEntry:
        p: list[str] = []
        if self.redacted != is_secret_key(self.key):
            p.append("a value is redacted exactly when its key names a secret")
        if self.redacted:
            if (self.value, self.length, self.value_truncated) != (None, None, False):
                p.append("a redacted entry shows neither value nor length")
        elif self.value is None or self.length is None:
            p.append("an unredacted entry has its value and length")
        else:
            if len(self.value) != min(self.length, MAX_VALUE):
                p.append(f"value has {len(self.value)} characters, length says {self.length}")
            if self.value_truncated != (self.length > MAX_VALUE):
                p.append("value_truncated disagrees with the length")
        _fail(f"key {self.key}", p)
        return self


class ConfigMapData(_Strict):
    namespace: DnsLabel
    name: DnsSubdomain
    found: bool
    entries: Annotated[list[ConfigEntry], Field(max_length=MAX_CONFIGMAP_KEYS)]
    entry_count: NonNegInt
    total_keys: NonNegInt
    truncated: bool
    binary_keys: Annotated[
        list[Annotated[str, Field(max_length=253)]], Field(max_length=MAX_CONFIGMAP_KEYS)
    ]

    @model_validator(mode="after")
    def _evidence(self) -> ConfigMapData:
        p: list[str] = []
        _truncation(
            p,
            "entries",
            self.entry_count,
            len(self.entries),
            self.total_keys,
            self.truncated,
            MAX_CONFIGMAP_KEYS,
        )
        keys = [e.key for e in self.entries]
        if keys != sorted(set(keys)):
            p.append("entries must be sorted by key, without duplicates")
        if self.binary_keys != sorted(set(self.binary_keys)):
            p.append("binary_keys must be sorted, without duplicates")
        if not self.found and (self.entries or self.total_keys or self.binary_keys):
            p.append("a ConfigMap that was not found has no content")
        _fail("get_configmap", p)
        return self


# --- get_service ----------------------------------------------------------------------------------


class ServiceData(_Strict):
    namespace: DnsLabel
    name: DnsSubdomain
    found: bool
    selector: (
        Annotated[
            dict[Annotated[str, Field(max_length=317)], Annotated[str, Field(max_length=63)]],
            Field(max_length=MAX_SELECTOR),
        ]
        | None
    )
    ready_endpoint_count: NonNegInt | None
    endpoint_count: NonNegInt | None

    @model_validator(mode="after")
    def _evidence(self) -> ServiceData:
        p: list[str] = []
        counts = (self.ready_endpoint_count, self.endpoint_count)
        if not self.found and (self.selector is not None or counts != (None, None)):
            p.append("a Service that was not found has no selector or endpoints")
        if self.found:
            if None in counts:
                p.append("a found Service has its endpoint counts")
            elif self.ready_endpoint_count > self.endpoint_count:
                p.append("more ready endpoints than endpoints")
        _fail("get_service", p)
        return self


# --- the eight tools, the envelope, and a stored step's checks --------------------------------

# Tool name -> (argument model, result model). The closed set the record accepts (§11.3).
CONTRACTS: dict[str, tuple[type[_Strict], type[_Strict]]] = {
    "get_pods": (PodsArgs, PodsData),
    "get_events": (EventsArgs, EventsData),
    "get_logs": (LogsArgs, LogsData),
    "get_metrics": (MetricsArgs, MetricsData),
    "get_deployment": (NamedArgs, DeploymentData),
    "get_rollout_history": (NamedArgs, RolloutHistoryData),
    "get_configmap": (NamedArgs, ConfigMapData),
    "get_service": (NamedArgs, ServiceData),
}


class ToolError(Exception):
    """A tool call that did not produce a result. Never an empty success."""

    def __init__(self, kind: str, message: str) -> None:
        if kind not in ErrorKind.__args__:  # type: ignore[attr-defined]
            raise ValueError(f"unknown tool error kind {kind!r}")
        self.kind = kind
        self.message = message[:MAX_ERROR]
        super().__init__(f"{kind}: {self.message}")


class ErrorInfo(_Strict):
    kind: ErrorKind
    message: Annotated[str, Field(max_length=MAX_ERROR)]


class OkResult(_Strict):
    status: Literal["ok"]
    data: dict[str, Any]


class ErrorResult(_Strict):
    status: Literal["error"]
    error: ErrorInfo


def ok_result(data: dict) -> dict:
    return {"status": "ok", "data": data}


def error_result(exc: ToolError) -> dict:
    return {"status": "error", "error": {"kind": exc.kind, "message": exc.message}}


def parse_args(model: type[_Strict], args: dict) -> _Strict:
    """Validate model-supplied arguments, or raise ToolError(invalid_argument). Runs before
    any read, so rejected input never reaches the cluster."""
    try:
        return model.model_validate_json(canonical_json(args))
    except (ValidationError, ValueError, TypeError) as exc:
        detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        raise ToolError("invalid_argument", f"{model.__name__}: {detail}") from exc


def invoke(tool: Callable[..., dict], ctx, args: dict) -> dict:
    """Run one tool with model-supplied arguments and return the trajectory envelope: ok and
    the bounded result, or error and why. A bug (any other exception) is not a tool error
    and propagates; it is never shown to the agent as one."""
    try:
        if not isinstance(args, dict):
            raise ToolError("invalid_argument", "arguments must be an object")
        model, _ = CONTRACTS[tool.__name__]
        if set(args) != set(model.model_fields):
            raise ToolError(
                "invalid_argument",
                f"{tool.__name__} takes exactly {sorted(model.model_fields)}, got {sorted(args)}",
            )
        parse_args(model, args)
        return ok_result(tool(ctx, **args))
    except ToolError as exc:
        return error_result(exc)


def step_problems(tool: str, args: Any, result: Any, digest: str) -> list[str]:
    """What is wrong with one stored trajectory step (run record version 2): the tool is one
    of the eight; the digest is the canonical digest of the stored result; the result is a
    well-formed envelope; a success carries a valid result that echoes valid arguments; a
    failure other than invalid_argument was attempted only with valid arguments."""
    p: list[str] = []
    if tool not in CONTRACTS:
        return [f"unknown tool {tool!r}"]
    try:
        expected = result_digest(result)
    except (TypeError, ValueError) as exc:
        return [f"{tool}: the result is not canonical JSON ({exc})"]
    if digest != expected:
        p.append(f"{tool}: result_digest is not the digest of the stored result")
    args_model, data_model = CONTRACTS[tool]
    status = result.get("status") if isinstance(result, dict) else None
    envelope = {"ok": OkResult, "error": ErrorResult}.get(status)  # type: ignore[arg-type]
    if envelope is None:
        return p + [f"{tool}: the result has no ok or error status"]
    try:
        parsed = envelope.model_validate_json(canonical_json(result))
    except ValidationError as exc:
        return p + [f"{tool}: malformed {status} envelope: {exc.errors()[0]['msg']}"]
    try:
        valid_args = args_model.model_validate_json(canonical_json(args))
    except (ValidationError, TypeError, ValueError):
        valid_args = None
    if isinstance(parsed, ErrorResult):
        if parsed.error.kind != "invalid_argument" and valid_args is None:
            p.append(f"{tool}: a {parsed.error.kind} failure was attempted with invalid arguments")
        return p
    if valid_args is None:
        return p + [f"{tool}: a successful call with invalid arguments"]
    try:
        data_model.model_validate_json(canonical_json(parsed.data))
    except ValidationError as exc:
        return p + [f"{tool}: invalid result: {exc.errors()[0]['msg']}"]
    for name, value in valid_args.model_dump(mode="json").items():
        if parsed.data.get(name) != value:
            p.append(f"{tool}: the result's {name} does not echo the argument")
    return p
