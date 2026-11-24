"""Steady-state measurement: the window accounting (testbed/app/window.py) and the
harness-side rule, both tested against fabricated attempts (decision 0009).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import math
import sys
from pathlib import Path

import pytest

from safeops.environment.workload import (
    WORKLOAD_CONFIG_PATH,
    SteadyState,
    SteadyStateMeasurementFailed,
    SteadyStateNotReached,
    WorkloadGenerator,
    WorkloadSettings,
    check_steady_state,
    load_workload_settings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# window.py is stdlib-only by design, so it loads without the image's dependencies.
_spec = importlib.util.spec_from_file_location(
    "testbed_window", REPO_ROOT / "testbed/app/window.py"
)
window = importlib.util.module_from_spec(_spec)
sys.modules["testbed_window"] = window  # dataclasses need the module registered
_spec.loader.exec_module(window)

DEADLINE = 1.0


def _fill(w, outcomes, rate=10.0):
    """outcomes[i] = (status, latency_s) or None for no response."""
    for i, outcome in enumerate(outcomes):
        due = i / rate
        a = window.Attempt(seq=i, due=due, dispatched=due)
        if outcome is not None:
            a.status, a.completed = outcome[0], due + outcome[1]
        assert w.admit(a)
    return w


def _closed(w):
    return max(a.dispatched for a in w.attempts) + w.deadline_s + w.settle_s


def test_config_values_match_the_decision() -> None:
    s = load_workload_settings()
    assert (s.rate_rps, s.window_attempts, s.min_successes, s.attempt_deadline_s) == (
        10,
        300,
        297,
        1.0,
    )
    assert s.window_attempts / s.rate_rps == 30  # 300 attempts over 30 s


def test_perfect_window_counts_all_300() -> None:
    w = _fill(window.Window("w1", 300, DEADLINE), [(200, 0.02)] * 300)
    out = w.summary(_closed(w))
    assert out["state"] == "complete"
    assert (out["scheduled"], out["succeeded"], out["failed"]) == (300, 300, 0)
    assert out["success_rate"] == 1.0
    assert out["p95_latency_ms"] == 20.0


def test_denominator_is_always_the_window_size() -> None:
    outcomes = [(200, 0.02)] * 297 + [(500, 0.02), None, (200, 1.5)]
    w = _fill(window.Window("w1", 300, DEADLINE), outcomes)
    out = w.summary(_closed(w))
    assert (out["succeeded"], out["failed"]) == (297, 3)
    assert out["success_rate"] == 297 / 300
    assert out["timed_out_or_no_response"] == 2  # no response + late 200
    assert out["error_responses"] == 1  # the 500


def test_late_200_is_a_failure_even_after_it_arrives() -> None:
    w = _fill(window.Window("w1", 1, DEADLINE), [(200, DEADLINE + 0.001)])
    assert w.summary(_closed(w) + 60)["succeeded"] == 0


def test_200_exactly_at_the_deadline_succeeds() -> None:
    w = _fill(window.Window("w1", 1, DEADLINE), [(200, DEADLINE)])
    assert w.summary(_closed(w))["succeeded"] == 1


def test_window_stays_draining_until_last_deadline_and_settle_pass() -> None:
    w = _fill(window.Window("w1", 300, DEADLINE), [(200, 0.02)] * 300)
    last = max(a.dispatched for a in w.attempts)
    assert w.state(last + DEADLINE) == window.DRAINING
    assert w.state(last + DEADLINE + w.settle_s) == window.COMPLETE
    assert "succeeded" not in w.summary(last)  # no partial numbers while draining


def test_outstanding_attempt_at_close_is_a_failure() -> None:
    w = _fill(window.Window("w1", 300, DEADLINE), [(200, 0.02)] * 299 + [None])
    assert w.summary(_closed(w))["failed"] == 1


def test_unfilled_window_is_a_measurement_failure_not_a_smaller_denominator() -> None:
    w = _fill(window.Window("w1", 300, DEADLINE), [(200, 0.02)] * 120)
    assert w.state(1e9) == window.COLLECTING
    w.fail("generator stopped before the window was filled")
    out = w.summary(1e9)
    assert out["state"] == window.FAILED
    assert out["scheduled"] == 120
    assert "success_rate" not in out and "succeeded" not in out


def test_full_window_admits_nothing_more() -> None:
    w = _fill(window.Window("w1", 2, DEADLINE), [(200, 0.01)] * 2)
    assert not w.admit(window.Attempt(seq=9, due=1.0, dispatched=1.0))
    w.fail("late failure")  # a full window can no longer fail
    assert w.failure is None


# --- harness-side rule ------------------------------------------------------------------


def _ss(state="complete", scheduled=300, succeeded=297) -> SteadyState:
    return SteadyState("w1", state, 300, scheduled, succeeded, 297, None, None)


def test_baseline_passes_at_297_of_300() -> None:
    check_steady_state(_ss(succeeded=297))
    check_steady_state(_ss(succeeded=300))


def test_baseline_fails_at_296_of_300() -> None:
    with pytest.raises(SteadyStateNotReached, match="296 of 300"):
        check_steady_state(_ss(succeeded=296))


@pytest.mark.parametrize("state,scheduled", [("failed", 120), ("timeout", 300), ("complete", 299)])
def test_unmeasured_window_is_a_measurement_failure(state: str, scheduled: int) -> None:
    with pytest.raises(SteadyStateMeasurementFailed):
        check_steady_state(_ss(state=state, scheduled=scheduled, succeeded=None))


def test_settings_reject_impossible_threshold() -> None:
    with pytest.raises(ValueError):
        WorkloadSettings("/checkout", 10, 1.0, 15, 300, 301, 60, 1, 120)


class FakeProxy:
    def __init__(self, summaries):
        self.controls, self.summaries = [], list(summaries)

    def control(self, body):
        self.controls.append(body)
        return {"window_id": "w7"} if body["action"] == "measure" else {}

    def stats(self, window_id=None):
        assert window_id == "w7"
        return self.summaries.pop(0) if len(self.summaries) > 1 else self.summaries[0]


SETTINGS = WorkloadSettings("/checkout", 10, 1.0, 15, 300, 297, 60, 1, 120)


def test_generator_drives_start_measure_stop_and_waits_for_warmup() -> None:
    clock = {"t": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    complete = {
        "state": "complete",
        "size": 300,
        "scheduled": 300,
        "succeeded": 299,
        "failed": 1,
        "deadline_s": 1.0,
        "success_rate": 299 / 300,
        "p95_latency_ms": 12.0,
    }
    proxy = FakeProxy([{"state": "collecting", "size": 300, "scheduled": 10}, complete])
    gen = WorkloadGenerator(proxy, SETTINGS, clock=lambda: clock["t"], sleep=sleep)
    gen.start_workload()
    result = gen.verify_steady_state()
    gen.stop_workload()
    assert [c["action"] for c in proxy.controls] == ["start", "measure", "stop"]
    assert proxy.controls[0] == {
        "action": "start",
        "rate_rps": 10,
        "deadline_s": 1.0,
        "path": "/checkout",
    }
    assert proxy.controls[1] == {"action": "measure", "attempts": 300}
    assert slept[0] == 15  # warmup before the window opens
    assert result.baseline_ok and result.succeeded == 299


def test_generator_times_out_into_a_measurement_failure() -> None:
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    proxy = FakeProxy([{"state": "collecting", "size": 300, "scheduled": 5}])
    gen = WorkloadGenerator(proxy, SETTINGS, clock=lambda: clock["t"], sleep=sleep)
    gen.start_workload()
    result = gen.verify_steady_state()
    assert result.state == "timeout"
    with pytest.raises(SteadyStateMeasurementFailed):
        check_steady_state(result)


def test_generator_rejects_a_window_of_the_wrong_size() -> None:
    proxy = FakeProxy([{"state": "complete", "size": 100, "scheduled": 100, "succeeded": 100}])
    gen = WorkloadGenerator(proxy, SETTINGS, clock=lambda: 1e6, sleep=lambda s: None)
    gen._started_at = 0.0
    with pytest.raises(SteadyStateMeasurementFailed):
        check_steady_state(gen.verify_steady_state())


def test_verify_before_start_is_a_measurement_failure() -> None:
    gen = WorkloadGenerator(FakeProxy([{}]), SETTINGS)
    with pytest.raises(SteadyStateMeasurementFailed):
        gen.verify_steady_state()


def test_construction_has_no_side_effects() -> None:
    WorkloadGenerator()  # no config read, no cluster contact


# --- fail-closed numeric settings (pre-Milestone-8 hardening) ----------------------------------
#
# A comparison against NaN is always false, so before this hardening malformed values could
# pass the relationship checks. Each timing field now has its own explicit contract, checked
# before anything is derived from it. The relationship checks below it are unchanged.

LOADED = load_workload_settings()
POSITIVE = ("rate_rps", "attempt_deadline_s", "window_poll_timeout_s", "window_poll_interval_s")


def _with(**changes) -> WorkloadSettings:
    return dataclasses.replace(LOADED, **changes)


@pytest.mark.parametrize("field", POSITIVE)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_each_positive_timing_field_must_be_finite_and_positive(field, value) -> None:
    with pytest.raises(ValueError, match=f"^{field} must be a finite number > 0"):
        _with(**{field: value})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_non_finite_warmup_is_rejected(value) -> None:
    """Before: NaN and -inf skipped the warmup sleep silently; +inf failed in sleep()."""
    with pytest.raises(ValueError, match="^warmup_s must be a finite number >= 0"):
        _with(warmup_s=value)


def test_a_negative_warmup_is_rejected() -> None:
    """Before: a negative warmup silently skipped the warmup sleep."""
    with pytest.raises(ValueError, match="^warmup_s must be a finite number >= 0"):
        _with(warmup_s=-1.0)


def test_a_zero_warmup_is_still_valid() -> None:
    assert _with(warmup_s=0.0).warmup_s == 0.0


def test_a_nan_rate_can_no_longer_reach_a_zero_window_verdict() -> None:
    """Before: rate NaN was accepted, the window duration was NaN, both relationship checks
    failed open, and the verifier measured zero windows and returned effective=false."""
    with pytest.raises(ValueError, match="^rate_rps must be"):
        _with(rate_rps=math.nan)


def test_an_infinite_rate_is_rejected_at_its_own_field() -> None:
    """+inf gives a finite but meaningless window duration (2.5 s), so only the field's own
    contract can refuse it."""
    assert 300 / math.inf + LOADED.attempt_deadline_s + 1.5 == 2.5
    with pytest.raises(ValueError, match="^rate_rps must be a finite number > 0"):
        _with(rate_rps=math.inf)


def test_an_infinite_attempt_deadline_is_rejected_at_its_own_field() -> None:
    """Already refused before, but only through the derived window duration and the poll
    timeout. Now it is refused by its own contract."""
    with pytest.raises(ValueError, match="^attempt_deadline_s must be a finite number > 0"):
        _with(attempt_deadline_s=math.inf)


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_a_poll_timeout_that_would_disable_the_poll_deadline_is_rejected(value) -> None:
    """Before: accepted; `clock() > deadline` was never true, so a window that never closed
    could be polled without bound."""
    with pytest.raises(ValueError, match="^window_poll_timeout_s must be a finite number > 0"):
        _with(window_poll_timeout_s=value)


@pytest.mark.parametrize("value", [0.0, -1.0, -math.inf])
def test_a_non_positive_poll_timeout_is_rejected_by_its_own_contract(value) -> None:
    """Already refused before, incidentally, because the window duration exceeded it. Now
    refused at the field itself, before any window duration is compared with it."""
    with pytest.raises(ValueError, match="^window_poll_timeout_s must be a finite number > 0"):
        _with(window_poll_timeout_s=value)


def test_a_valid_poll_timeout_bounds_a_window_that_never_closes() -> None:
    """The time-based exit the field contract protects: a window that never reports
    complete or failed ends as a timeout once the configured poll timeout passes."""
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    running = {"state": "running", "scheduled": 10}
    g = WorkloadGenerator(FakeProxy([running]), LOADED, clock=lambda: clock["t"], sleep=sleep)
    g._started_at = 0.0
    m = g.measure_recovery_window()
    assert m.state == "timeout"
    assert clock["t"] <= LOADED.window_poll_timeout_s + 2 * LOADED.window_poll_interval_s


def test_finite_but_unreasonable_values_still_fail_the_relationship_checks() -> None:
    with pytest.raises(ValueError, match="window poll timeout"):
        _with(rate_rps=0.001)  # a 300-attempt window would take 300,000 s
    with pytest.raises(ValueError, match="window poll timeout"):
        _with(attempt_deadline_s=1e308, window_poll_timeout_s=1e308)  # duration == timeout
    assert math.isinf(_inf_duration := 300 / 1e-306 + 1.0 + 1.5)  # finite inputs, inf duration
    with pytest.raises(ValueError, match="window poll timeout"):
        _with(rate_rps=1e-306)
    with pytest.raises(ValueError, match="recovery_timeout_s"):
        _with(recovery_timeout_s=LOADED.closes_after_s(300) - 0.1)
    assert _with(recovery_timeout_s=LOADED.closes_after_s(300))  # exactly one window


def test_an_oversized_finite_poll_interval_is_left_alone() -> None:
    """Deliberately out of scope: no interval <= timeout constraint."""
    assert _with(window_poll_interval_s=600.0).window_poll_interval_s == 600.0


def test_the_loaded_settings_are_unchanged_by_the_hardening() -> None:
    assert LOADED == WorkloadSettings(
        target_path="/checkout",
        rate_rps=10.0,
        attempt_deadline_s=1.0,
        warmup_s=15.0,
        window_attempts=300,
        min_successes=297,
        window_poll_timeout_s=60.0,
        window_poll_interval_s=1.0,
        recovery_timeout_s=120.0,
    )
    assert LOADED.closes_after_s(LOADED.window_attempts) == 32.5


# --- integer settings are integers (second pre-Milestone-8 hardening) --------------------------
#
# The loader used to int()-coerce window_attempts and min_successes before WorkloadSettings
# saw them: 297.9 became 297, "297" became 297, and min_successes: true became a threshold of
# 1. Now the loader passes them through as read and WorkloadSettings requires type int.


def _load_with(tmp_path, **changes) -> WorkloadSettings:
    import yaml

    raw = yaml.safe_load(WORKLOAD_CONFIG_PATH.read_text())
    path = tmp_path / "workload.yaml"
    path.write_text(yaml.safe_dump({**raw, **changes}))
    return load_workload_settings(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_attempts", 301.9),
        ("min_successes", 297.9),
        ("window_attempts", 300.0),
        ("min_successes", 297.0),
        ("window_attempts", "300"),
        ("min_successes", "297"),
        ("window_attempts", True),
        ("min_successes", True),
    ],
)
def test_the_loader_never_coerces_an_integer_setting(tmp_path, field, value) -> None:
    with pytest.raises(ValueError, match=f"^{field} must be an integer"):
        _load_with(tmp_path, **{field: value})


def test_a_fractional_threshold_is_not_truncated_by_the_loader(tmp_path) -> None:
    """Before: 297.9 -> int() -> 297, accepted."""
    with pytest.raises(ValueError, match="^min_successes must be an integer, got 297.9"):
        _load_with(tmp_path, min_successes=297.9)


def test_a_boolean_threshold_never_becomes_one() -> None:
    """Before: min_successes: true -> int(True) == 1, so one success out of 300 passed."""
    with pytest.raises(ValueError, match="^min_successes must be an integer, got True"):
        _with(min_successes=True)
    with pytest.raises(ValueError, match="^min_successes must be an integer"):
        _with(min_successes=False)


def test_a_boolean_window_is_named_as_itself() -> None:
    """Before: rejected only through the relationship check, as if min_successes were wrong."""
    with pytest.raises(ValueError, match="^window_attempts must be an integer, got True"):
        _with(window_attempts=True)


@pytest.mark.parametrize("value", [300.5, 300.0, math.nan, math.inf, "300"])
def test_the_constructor_requires_an_int_window(value) -> None:
    with pytest.raises(ValueError, match="^window_attempts must be an integer"):
        _with(window_attempts=value)


@pytest.mark.parametrize("value", [297.5, 297.0, math.nan, math.inf, "297"])
def test_the_constructor_requires_an_int_threshold(value) -> None:
    with pytest.raises(ValueError, match="^min_successes must be an integer"):
        _with(min_successes=value)


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_int_window_reaches_the_positivity_check(value) -> None:
    """The type guard passes a real int on to the range checks: none of them is dead."""
    with pytest.raises(ValueError, match="^window_attempts must be positive$"):
        _with(window_attempts=value)


@pytest.mark.parametrize("value", [0, -1, 301])
def test_an_out_of_range_int_threshold_reaches_the_relationship_check(value) -> None:
    with pytest.raises(ValueError, match=r"^min_successes must be in 1\.\.window_attempts$"):
        _with(min_successes=value)


def test_valid_integer_settings_are_unchanged() -> None:
    assert _with(window_attempts=300, min_successes=297).min_successes == 297
    assert _with(window_attempts=297, min_successes=297).window_attempts == 297  # equality ok
    assert type(LOADED.window_attempts) is int and type(LOADED.min_successes) is int
    for name in (
        "rate_rps",
        "attempt_deadline_s",
        "warmup_s",
        "window_poll_timeout_s",
        "window_poll_interval_s",
        "recovery_timeout_s",
    ):
        assert type(getattr(LOADED, name)) is float, name  # the loader's widening is intact
