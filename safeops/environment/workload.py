"""Workload generator control and the steady-state baseline (design §5; decision 0009).

The harness reads workload measurements straight from the generator, over the
experiment-controller proxy path, never through Prometheus. This is the measurement
`effective` is later judged against (§11.7). Prometheus may show workload metrics to
the agent, but no verdict depends on it.

Import policy (decision 0004): this module is reachable from RUN_SEQUENCE, so it
imports no cluster or provider client at module scope. safeops.cluster and PyYAML are
imported inside the methods that need them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

WORKLOAD_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "workload.yaml"


class SteadyStateError(Exception):
    """A pre-run failure: no valid steady-state baseline. The run is discarded (decision 0009)."""


class SteadyStateMeasurementFailed(SteadyStateError):
    """The window couldn't be measured: not all attempts scheduled, generator lost, timed out."""


class WorkloadGeneratorBusy(SteadyStateMeasurementFailed):
    """The generator is already running: another run is using it, or a previous run died
    without stopping it. Never stopped automatically, since that could clobber a
    concurrent run. `safeops workload-stop` clears it (decision 0009)."""


class SteadyStateNotReached(SteadyStateError):
    """The window was measured, but too few of its scheduled attempts succeeded."""


@dataclass(frozen=True)
class WorkloadSettings:
    target_path: str
    rate_rps: float
    attempt_deadline_s: float
    warmup_s: float
    window_attempts: int
    min_successes: int
    window_poll_timeout_s: float
    window_poll_interval_s: float

    def __post_init__(self) -> None:
        if not 0 < self.min_successes <= self.window_attempts:
            raise ValueError("min_successes must be in 1..window_attempts")
        if self.rate_rps <= 0 or self.attempt_deadline_s <= 0 or self.window_attempts <= 0:
            raise ValueError("rate, deadline and window size must be positive")


def load_workload_settings(path: Path = WORKLOAD_CONFIG_PATH) -> WorkloadSettings:
    import yaml

    data = yaml.safe_load(path.read_text())
    return WorkloadSettings(
        target_path=str(data["target_path"]),
        rate_rps=float(data["rate_rps"]),
        attempt_deadline_s=float(data["attempt_deadline_s"]),
        warmup_s=float(data["warmup_s"]),
        window_attempts=int(data["window_attempts"]),
        min_successes=int(data["min_successes"]),
        window_poll_timeout_s=float(data["window_poll_timeout_s"]),
        window_poll_interval_s=float(data["window_poll_interval_s"]),
    )


@dataclass(frozen=True)
class SteadyState:
    """One measured window. The denominator is always the full window size."""

    window_id: str | None
    state: str  # complete | failed | timeout
    size: int
    scheduled: int
    succeeded: int | None
    min_successes: int
    success_rate: float | None
    p95_latency_ms: float | None
    failure: str | None = None

    @property
    def measured(self) -> bool:
        return self.state == "complete" and self.scheduled == self.size

    @property
    def baseline_ok(self) -> bool:
        return self.measured and self.succeeded is not None and self.succeeded >= self.min_successes


def check_steady_state(steady_state: SteadyState) -> None:
    """Raise unless the window is a valid baseline. The runner and the CLI share this rule."""
    if not steady_state.measured:
        raise SteadyStateMeasurementFailed(
            f"steady-state window {steady_state.window_id} not measured"
            f" (state={steady_state.state}, scheduled {steady_state.scheduled}"
            f" of {steady_state.size}): {steady_state.failure or 'no detail'}"
        )
    if not steady_state.baseline_ok:
        raise SteadyStateNotReached(
            f"steady-state baseline not reached: {steady_state.succeeded} of"
            f" {steady_state.size} scheduled attempts succeeded; need {steady_state.min_successes}"
        )


def _inconsistency(summary: dict, s: WorkloadSettings, size: int | None = None) -> str | None:
    """Why a 'complete' window summary can't be right, or None if it's consistent."""

    def is_int(v):
        return isinstance(v, int) and not isinstance(v, bool)

    expected = s.window_attempts if size is None else size
    size, scheduled = summary.get("size"), summary.get("scheduled")
    succeeded, failed = summary.get("succeeded"), summary.get("failed")
    if size != expected:
        return f"window size {size!r}, configured {expected}"
    if scheduled != size:
        return f"scheduled {scheduled!r} of {size}"
    if not (is_int(succeeded) and is_int(failed)):
        return "succeeded/failed missing or not integers"
    if not 0 <= succeeded <= size or succeeded + failed != size:
        return f"succeeded {succeeded} + failed {failed} != size {size}"
    if summary.get("deadline_s") != s.attempt_deadline_s:
        got = summary.get("deadline_s")
        return f"measured with deadline {got!r}s, configured {s.attempt_deadline_s}s"
    return None


def _server_error_inconsistency(summary: dict) -> str | None:
    """The 5xx count a fault-detection window needs (decision 0015) must fit the others."""
    errors, failed = summary.get("server_error_responses"), summary.get("failed")
    if not isinstance(errors, int) or isinstance(errors, bool):
        return "server_error_responses missing or not an integer"
    if not 0 <= errors <= failed:
        return f"server_error_responses {errors} outside 0..failed ({failed})"
    responded = summary.get("error_responses")
    if isinstance(responded, int) and errors > responded:
        return f"server_error_responses {errors} > error_responses {responded}"
    return None


@dataclass(frozen=True)
class WindowMeasurement:
    """One fixed-denominator window, cross-checked. ``summary`` is set only if measured."""

    window_id: str | None
    state: str  # complete | failed | timeout
    size: int
    scheduled: int
    summary: dict | None
    failure: str | None = None

    @property
    def measured(self) -> bool:
        return self.state == "complete" and self.summary is not None


class WorkloadGenerator:
    """Harness control of the in-cluster workload generator. Construction has no side effects."""

    def __init__(
        self,
        proxy=None,
        settings: WorkloadSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._proxy_obj = proxy
        self._settings = settings
        self._clock = clock
        self._sleep = sleep
        self._started_at: float | None = None

    @property
    def settings(self) -> WorkloadSettings:
        if self._settings is None:
            self._settings = load_workload_settings()
        return self._settings

    def _proxy(self):
        if self._proxy_obj is None:
            from safeops.cluster import WorkloadProxy, load_cluster_config

            self._proxy_obj = WorkloadProxy(load_cluster_config())
        return self._proxy_obj

    def start_workload(self) -> None:
        """Start open-loop load against gateway."""
        from safeops.cluster import CommandError

        s = self.settings
        try:
            self._proxy().control(
                {
                    "action": "start",
                    "rate_rps": s.rate_rps,
                    "deadline_s": s.attempt_deadline_s,
                    "path": s.target_path,
                }
            )
        except CommandError as exc:
            if "Conflict" in str(exc) or "already running" in str(exc):
                raise WorkloadGeneratorBusy(
                    "workload generator is already running (another run, or a previous run"
                    " that died); run `safeops workload-stop` if no other run is active"
                ) from exc
            raise
        self._started_at = self._clock()

    def _collect(self, attempts: int) -> WindowMeasurement:
        """Open a window of ``attempts`` scheduled attempts and wait for it to close.

        The generator's summary isn't trusted: every count is cross-checked, and a summary
        that can't be right is a measurement failure (review W1-W4). Losing the generator or
        its window mid-measurement is also a measurement failure (W6).
        """
        from safeops.cluster import CommandError

        s = self.settings
        try:
            window_id = self._proxy().control({"action": "measure", "attempts": attempts})[
                "window_id"
            ]
            deadline = self._clock() + s.window_poll_timeout_s
            while True:
                summary = self._proxy().stats(window_id)
                if summary.get("state") in ("complete", "failed"):
                    break
                if self._clock() > deadline:
                    return WindowMeasurement(
                        window_id,
                        "timeout",
                        attempts,
                        int(summary.get("scheduled", 0)),
                        None,
                        f"window not closed after {s.window_poll_timeout_s}s",
                    )
                self._sleep(s.window_poll_interval_s)
        except CommandError as exc:
            return WindowMeasurement(
                None,
                "failed",
                attempts,
                0,
                None,
                f"generator or window lost during measurement: {exc}",
            )

        scheduled = int(summary.get("scheduled") or 0)
        if summary.get("state") == "failed":
            return WindowMeasurement(
                window_id, "failed", attempts, scheduled, None, summary.get("failure") or "failed"
            )
        problem = _inconsistency(summary, s, attempts)
        if problem is not None:
            return WindowMeasurement(
                window_id, "failed", attempts, scheduled, None, f"inconsistent summary: {problem}"
            )
        return WindowMeasurement(window_id, "complete", attempts, scheduled, dict(summary))

    def verify_steady_state(self) -> SteadyState:
        """Measure one fixed-denominator window after the warmup. Doesn't judge it.

        The runner's _require_steady_state hook and the CLI apply check_steady_state().
        """
        s = self.settings
        if self._started_at is None:
            raise SteadyStateMeasurementFailed("verify_steady_state called before start_workload")
        remaining = self._started_at + s.warmup_s - self._clock()
        if remaining > 0:
            self._sleep(remaining)
        m = self._collect(s.window_attempts)
        if not m.measured:
            return SteadyState(
                m.window_id,
                m.state,
                s.window_attempts,
                m.scheduled,
                None,
                s.min_successes,
                None,
                None,
                m.failure,
            )
        summary = m.summary
        return SteadyState(
            window_id=m.window_id,
            state="complete",
            size=s.window_attempts,
            scheduled=summary["scheduled"],
            succeeded=summary["succeeded"],
            min_successes=s.min_successes,
            success_rate=summary.get("success_rate"),
            p95_latency_ms=summary.get("p95_latency_ms"),
            failure=None,
        )

    def measure_window(self, attempts: int) -> WindowMeasurement:
        """Measure one more window while load is running (fault detection; decision 0015).

        Requires the 5xx count as well as the steady-state cross-checks. Doesn't judge it.
        """
        if self._started_at is None:
            return WindowMeasurement(None, "failed", attempts, 0, None, "load is not running")
        m = self._collect(attempts)
        if m.measured:
            problem = _server_error_inconsistency(m.summary)
            if problem is not None:
                return WindowMeasurement(
                    m.window_id,
                    "failed",
                    attempts,
                    m.scheduled,
                    None,
                    f"inconsistent summary: {problem}",
                )
        return m

    def stop_workload(self) -> None:
        """Stop the load generator."""
        self._proxy().control({"action": "stop"})
        self._started_at = None
