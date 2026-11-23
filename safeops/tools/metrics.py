"""get_metrics (design §11.3; decisions 0010, 0017 §4, 0023).

`metric` names one of five fixed PromQL templates; `service` must be a topology service and
only fills a label value or a pod-name pattern built from config/topology.yaml. No argument
ever becomes PromQL text of its own.

Current incident only (0017 §4): every template is a rate or a gauge, never a raw counter,
and the first point is placed at least one rate range after the run's load start, so no
point's rate reaches back into an earlier run. The step is chosen so the window holds at
most 60 points (the §11.3 cap); a response that still exceeds it keeps the newest 60 and
says so.

Non-finite samples (Prometheus "NaN", "+Inf", "-Inf", e.g. an error ratio with no traffic)
become the canonical tag at this boundary (safeops.tools.canonical), never 0 or a dropped
point.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from string import Template
from urllib.parse import urlencode

from safeops.tools import contracts as c
from safeops.tools.canonical import non_finite
from safeops.tools.contracts import ToolError
from safeops.tools.kube import _projected, _result
from safeops.tools.reader import ToolContext

# $ns, $svc, $pods and $r are the only substitutions (string.Template; PromQL has no `$`).
_APP = 'namespace="$ns",app="$svc"'
_CADVISOR = 'namespace="$ns",pod=~"$pods",container!="",container!="POD"'
TEMPLATES: dict[str, Template] = {
    "request_rate": Template(f"sum(rate(http_requests_total{{{_APP}}}[${{r}}s]))"),
    "error_rate": Template(
        f'sum(rate(http_requests_total{{{_APP},code=~"5[0-9][0-9]"}}[${{r}}s]))'
        f" / sum(rate(http_requests_total{{{_APP}}}[${{r}}s]))"
    ),
    "latency_p95_seconds": Template(
        "histogram_quantile(0.95, sum by (le)"
        f" (rate(http_request_duration_seconds_bucket{{{_APP}}}[${{r}}s])))"
    ),
    "cpu_usage_cores": Template(
        f"sum(rate(container_cpu_usage_seconds_total{{{_CADVISOR}}}[${{r}}s]))"
    ),
    "memory_working_set_bytes": Template(f"sum(container_memory_working_set_bytes{{{_CADVISOR}}})"),
}


def pod_pattern(ctx: ToolContext, service: str) -> str:
    """An anchored regex for the service's pods, from its workloads in the topology:
    `<deployment>-<hash>-<hash>` or `<statefulset>-<ordinal>`."""
    parts = []
    for spec in sorted(ctx.topology.resources.values(), key=lambda s: s.name):
        if spec.owner != service:
            continue
        if spec.kind == "Deployment":
            parts.append(f"{spec.name}-[a-z0-9]+-[a-z0-9]+")
        elif spec.kind == "StatefulSet":
            parts.append(f"{spec.name}-[0-9]+")
    if not parts:
        raise ToolError("invalid_argument", f"service {service!r} owns no workload")
    return "|".join(parts)


def query(ctx: ToolContext, service: str, metric: str, range_s: int) -> str:
    return TEMPLATES[metric].substitute(
        ns=ctx.namespace, svc=service, pods=pod_pattern(ctx, service), r=str(range_s)
    )


def plan(window: int, end: int, not_before: datetime) -> tuple[int, int, int]:
    """(start, step_s, range_s), in whole seconds, for a window ending at `end`."""
    span = window * 60
    nominal_step = max(c.METRIC_MIN_STEP_S, math.ceil(span / (c.MAX_METRIC_POINTS - 1)))
    range_s = max(c.METRIC_MIN_RANGE_S, nominal_step)
    floor = math.ceil(not_before.timestamp()) + range_s
    start = max(end - span, floor)
    step_s = max(c.METRIC_MIN_STEP_S, math.ceil(max(end - start, 0) / (c.MAX_METRIC_POINTS - 1)))
    return start, step_s, range_s


def sample_value(raw: object) -> float | dict:
    """One Prometheus sample value (always a string in the API) as a finite float or the
    canonical non-finite tag."""
    if not isinstance(raw, str):
        raise ValueError(f"a sample value is a string, got {type(raw).__name__}")
    special = {"NaN": math.nan, "+Inf": math.inf, "Inf": math.inf, "-Inf": -math.inf}
    value = special[raw] if raw in special else float(raw)
    return value if math.isfinite(value) else non_finite(value)


def _points(body: dict, start: int, end: int, step_s: int) -> list[dict]:
    if body.get("status") != "success":
        raise ToolError("command_failed", f"Prometheus: {str(body.get('error'))[:300]}")
    data = body["data"]
    if data.get("resultType") != "matrix":
        raise ValueError(f"expected a matrix, got {data.get('resultType')!r}")
    series = data["result"]
    if len(series) > 1:  # every template aggregates to one series
        raise ValueError(f"{len(series)} series where the template yields at most one")
    points = []
    for ts, raw in series[0]["values"] if series else []:
        if isinstance(ts, bool) or not isinstance(ts, int | float) or ts != int(ts):
            raise ValueError(f"sample time {ts!r} is not a whole second")
        ts = int(ts)
        if not start <= ts <= end or (ts - start) % step_s:
            raise ValueError(f"sample time {ts} is off the requested grid")
        points.append({"t": datetime.fromtimestamp(ts, UTC), "v": sample_value(raw)})
    return points


def get_metrics(ctx: ToolContext, service: str, metric: str, window: int) -> dict:
    """`metric` for `service` over the last `window` minutes of the current incident, at most
    60 points, oldest first."""
    args = c.parse_args(c.MetricsArgs, {"service": service, "metric": metric, "window": window})
    ctx.require_service(args.service)
    end = int(ctx.now().timestamp())
    start, step_s, range_s = plan(args.window, end, ctx.not_before)
    points: list[dict] = []
    if start <= end:  # otherwise the incident is younger than one rate range: no points yet
        promql = query(ctx, args.service, args.metric, range_s)
        cfg = ctx.kubectl.cfg
        path = (
            f"/api/v1/namespaces/{cfg.monitoring_namespace}/services/"
            f"{cfg.prometheus_service}:{cfg.prometheus_service_port}/proxy/api/v1/query_range?"
            + urlencode({"query": promql, "start": start, "end": end, "step": step_s})
        )
        body = ctx.raw_get(path)
        points = _projected(lambda: _points(body, start, end, step_s))
    shown = points[-c.MAX_METRIC_POINTS :]
    return _result(
        c.MetricsData,
        service=args.service,
        metric=args.metric,
        window=args.window,
        unit=c.METRIC_UNITS[args.metric],
        start=datetime.fromtimestamp(start, UTC),
        end=datetime.fromtimestamp(end, UTC),
        step_s=step_s,
        range_s=range_s,
        points=shown,
        point_count=len(shown),
        truncated=len(points) > len(shown),
    )
