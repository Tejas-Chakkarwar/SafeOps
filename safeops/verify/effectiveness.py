"""Effectiveness verifier (design §11.7; decision 0024). Outside the trust boundary.

``effective`` is measured against the run's pre-fault steady state (decision 0024 §1), on
the workload generator that is already running (decision 0009), never Prometheus:

- **Recovered** means one successfully measured, complete decision-0009 window of the
  full configured size with ``succeeded >= steady_state.min_successes`` (§2). There is no
  second, relative tolerance.
- **Schedule** (§3): consecutive full windows inside ``timeout_s``, which starts when
  ``verify`` begins. A window starts only if it can close inside the remaining budget
  (``closes_after_s``). A started window is never truncated. The first passing window
  ends measurement; ``time_to_recovery_s`` is the monotonic time from the start of
  ``verify`` to that window's close.
- **A measurement failure is not ineffectiveness** (§4). A window that can't be validly
  measured, or that closes after the deadline (the late-window amendment), raises
  RecoveryMeasurementError: the run is an infrastructure_failure discard, never
  ``effective = false``.

Nothing here reads or computes harm (Rule 13).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from safeops.environment.workload import (
    SteadyState,
    WindowMeasurement,
    WorkloadGenerator,
    WorkloadSettings,
    _inconsistency,
)


class RecoveryMeasurementError(Exception):
    """A post-action recovery window couldn't be validly measured (decision 0024 §4).

    Deliberately not a SteadyStateError (§5): by now the pre-fault baseline exists, so the
    runner would attribute a SteadyStateError to the baseline stage (no_steady_state). This
    reaches _cause's default instead: infrastructure_failure, at stage effectiveness."""


@dataclass(frozen=True)
class EffectivenessResult:
    """The verdict and its recovery time, which can't disagree: ``effective`` exactly when
    ``time_to_recovery_s`` is a finite, non-negative number of seconds (decision 0024 §6)."""

    effective: bool
    time_to_recovery_s: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.effective, bool):
            raise TypeError("EffectivenessResult.effective must be a bool")
        t = self.time_to_recovery_s
        if not self.effective:
            if t is not None:
                raise ValueError("an ineffective run has no time_to_recovery_s (censored)")
            return
        if t is None or isinstance(t, bool) or not isinstance(t, int | float):
            raise ValueError("an effective run needs its time_to_recovery_s in seconds")
        if not math.isfinite(t) or t < 0:
            raise ValueError(f"time_to_recovery_s must be finite and non-negative, got {t!r}")
        object.__setattr__(self, "time_to_recovery_s", float(t))

    def to_record(self) -> dict:
        return {"effective": self.effective, "time_to_recovery_s": self.time_to_recovery_s}


def _window_problem(m: WindowMeasurement, s: WorkloadSettings) -> str | None:
    """Why ``m`` is not a valid, complete, full-size decision-0009 window, or None. The
    state string alone is not trusted: the counts are checked again here."""
    if m.state != "complete" or m.summary is None:
        return f"window {m.window_id} not measured (state={m.state}): {m.failure or 'no detail'}"
    if m.size != s.window_attempts or m.scheduled != m.size:
        return f"scheduled {m.scheduled} of {m.size}, configured {s.window_attempts}"
    return _inconsistency(m.summary, s, s.window_attempts)


class EffectivenessVerifier:
    """Measures recovery on the shared, already-running workload. Construction has no side
    effects; the clock is monotonic and injectable."""

    def __init__(
        self, *, workload: WorkloadGenerator, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.workload = workload
        self._clock = clock

    def verify(self, steady_state: SteadyState, timeout_s: float) -> EffectivenessResult:
        """Measure recovery for up to ``timeout_s`` seconds (decision 0024 §2-§4)."""
        if not isinstance(steady_state, SteadyState):
            raise TypeError("verify needs the run's pre-fault SteadyState")
        s = self.workload.settings
        if not (steady_state.baseline_ok and steady_state.size == s.window_attempts):
            # min_successes is a count out of the baseline's own window. Against a window of
            # another size, or a baseline that never passed, it would score recovery
            # against the wrong line without crashing (decision 0024 §1-§2).
            raise ValueError(
                f"steady state {steady_state.window_id} is not a passed baseline of this"
                f" {s.window_attempts}-attempt window (size {steady_state.size},"
                f" state {steady_state.state})"
            )
        window_s = s.closes_after_s(s.window_attempts)
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int | float)
            or not math.isfinite(timeout_s)
            or timeout_s < window_s
        ):
            # Zero windows would score the run ineffective without measuring anything; the
            # settings refuse such a budget, and so does the verifier (decision 0024 §3).
            raise ValueError(f"recovery budget {timeout_s!r}s can't fit one {window_s:g}s window")
        # One reading at entry is both the start and the first scheduling time, so a budget of
        # exactly one window always fits it. Each window's close is read on its own, for the
        # deadline and the recovery time; each later scheduling decision takes a fresh
        # reading, so time between windows counts against the budget (decision 0024 §3).
        start = schedule_time = self._clock()
        deadline = start + timeout_s
        while schedule_time + window_s <= deadline:
            m = self.workload.measure_recovery_window()
            close_time = self._clock()
            problem = _window_problem(m, s)
            if problem is not None:
                raise RecoveryMeasurementError(f"recovery window not validly measured: {problem}")
            if close_time > deadline:
                raise RecoveryMeasurementError(
                    f"recovery window {m.window_id} closed {close_time - deadline:g}s after the"
                    f" {timeout_s:g}s recovery deadline (decision 0024, late-window amendment)"
                )
            if m.summary["succeeded"] >= steady_state.min_successes:
                return EffectivenessResult(True, close_time - start)
            schedule_time = self._clock()
        return EffectivenessResult(False, None)
