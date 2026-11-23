"""Host and cluster clocks, compared twice per run (decision 0017 §6; Milestone 6, 0023).

One reading creates a fresh, uniquely named probe ConfigMap in the testbed through the
guarded runtime path (experiment-controller already creates and deletes ConfigMaps there,
so no new RBAC), and takes three timestamps:

    host_before     host wall clock immediately before the create request
    server_created  the new object's metadata.creationTimestamp (API server clock)
    host_after      host wall clock immediately after the create returned it

Each probe ConfigMap is deleted, and confirmed gone, before the reading is returned. The
first reading is taken, and its probe removed, before the measured workload begins. The
second is taken after every experimental measurement is complete: load generation may still
be flowing until the cleanup stops it, but the probe is never present during any measured
part of the run, the agent's execution, a snapshot, effectiveness measurement or harm
measurement (decision 0023 §13). Any failure to create, read or remove it raises
ClockProbeError: never a missing or synthesized reading.

What the numbers mean. creationTimestamp is the API-server clock as seen inside the kind
environment, truncated to the second (metav1.Time keeps whole seconds), plus request
latency. It is not a precise node clock. Its job is to catch offsets or jumps of seconds to
minutes. All arithmetic is in integer microseconds, so a record's derived values can be
recomputed exactly:

    offset_us   = server - (host_before + host_after) // 2      (latency is not offset)
    true offset in [server - host_after, server + 1 s - host_before)
    change_us   = offset_us(after) - offset_us(before)
    interpretation: "change_detected" when the change's interval excludes zero, i.e. the
    offset moved by more than the two readings' resolution and latency can explain;
    otherwise "no_change_detected". No threshold: the flagging bounds are Milestone 12's.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

PROBE_PREFIX = "safeops-clock-probe-"
PROBE_LABELS = {"app.kubernetes.io/part-of": "safeops", "safeops.io/role": "clock-probe"}
MAX_PROBE_ERROR = 500
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = timedelta(microseconds=1)
_SECOND_US = 1_000_000
CHANGE_DETECTED = "change_detected"
NO_CHANGE_DETECTED = "no_change_detected"


class ClockProbeError(Exception):
    """A clock reading could not be taken, or its probe could not be shown removed."""


def micros(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("clock timestamps are timezone-aware")
    return (dt - _EPOCH) // _US


@dataclass(frozen=True)
class ClockReading:
    host_before: datetime
    host_after: datetime
    server_created: datetime

    def __post_init__(self) -> None:
        for name in ("host_before", "host_after", "server_created"):
            micros(getattr(self, name))  # aware, or ValueError
        if self.server_created.microsecond:
            raise ValueError("creationTimestamp has whole-second resolution")

    @property
    def offset_us(self) -> int:
        return offset_us(self.host_before, self.host_after, self.server_created)

    def to_record(self) -> dict:
        return {
            "host_before": _iso(self.host_before),
            "host_after": _iso(self.host_after),
            "server_created": _iso(self.server_created),
            "offset_s": self.offset_us / _SECOND_US,
        }


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def offset_us(host_before: datetime, host_after: datetime, server_created: datetime) -> int:
    return micros(server_created) - (micros(host_before) + micros(host_after)) // 2


def offset_bounds_us(
    host_before: datetime, host_after: datetime, server_created: datetime
) -> tuple[int, int]:
    """[lo, hi): the true offset, given whole-second truncation and the request's window."""
    server = micros(server_created)
    return server - micros(host_after), server + _SECOND_US - micros(host_before)


def change_us(before: tuple, after: tuple) -> int:
    """Each argument is (host_before, host_after, server_created)."""
    return offset_us(*after) - offset_us(*before)


def interpretation(before: tuple, after: tuple) -> str:
    lo1, hi1 = offset_bounds_us(*before)
    lo2, hi2 = offset_bounds_us(*after)
    if lo2 - hi1 >= 0 or hi2 - lo1 <= 0:
        return CHANGE_DETECTED
    return NO_CHANGE_DETECTED


def clock_change(before: ClockReading, after: ClockReading) -> dict:
    """The derived record entry for two readings."""
    b = (before.host_before, before.host_after, before.server_created)
    a = (after.host_before, after.host_after, after.server_created)
    return {"change_s": change_us(b, a) / _SECOND_US, "interpretation": interpretation(b, a)}


def _parse_server_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"creationTimestamp {value!r} is not RFC 3339 UTC")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def take_reading(
    kubectl,
    namespace: str,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:16],
) -> ClockReading:
    """One reading through `kubectl` (the runtime harness client). Raises ClockProbeError."""
    name = PROBE_PREFIX + new_id()
    probe = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(PROBE_LABELS)},
    }
    try:
        host_before = wall_clock()
        stored = kubectl.create_returning(probe)
        host_after = wall_clock()
    except Exception as exc:  # it may exist anyway (a lost response): remove it if so
        _fail(kubectl, namespace, name, "could not create the clock probe", exc)
    try:
        meta = stored["metadata"]
        if meta.get("name") != name:
            raise ValueError(f"the API server returned {meta.get('name')!r}, not {name!r}")
        reading = ClockReading(
            host_before, host_after, _parse_server_time(meta["creationTimestamp"])
        )
    except Exception as exc:
        _fail(kubectl, namespace, name, "could not read the clock probe's creationTimestamp", exc)
    try:
        remove_probe(kubectl, namespace, name)
    except Exception as exc:
        raise ClockProbeError(_bounded(f"could not remove clock probe {name}", exc)) from exc
    return reading


def remove_probe(kubectl, namespace: str, name: str) -> None:
    """Delete the probe and confirm, by a fresh list, that it is gone."""
    kubectl.delete("configmap", "ConfigMap", name, namespace, timeout_s=60)
    left = kubectl.get_json(
        "configmaps", "-n", namespace, "--field-selector", f"metadata.name={name}"
    )
    if left.get("items"):
        raise ClockProbeError(f"clock probe {name} is still present after its delete")


def _fail(kubectl, namespace: str, name: str, what: str, exc: Exception):
    try:
        remove_probe(kubectl, namespace, name)
    except Exception as cleanup:
        exc.add_note(f"removing the probe also failed: {type(cleanup).__name__}: {cleanup}")
        what += f"; it may be left behind ({name}), for the next reset to remove"
    raise ClockProbeError(_bounded(what, exc)) from exc


def _bounded(what: str, exc: Exception) -> str:
    return f"{what}: {type(exc).__name__}: {exc}"[:MAX_PROBE_ERROR]
