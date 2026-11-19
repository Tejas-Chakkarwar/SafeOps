"""Steady-state measurement: the window accounting (testbed/app/window.py) and the
harness-side rule, both tested against fabricated attempts (decision 0009).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from safeops.environment.workload import (
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
        WorkloadSettings("/checkout", 10, 1.0, 15, 300, 301, 60, 1)


class FakeProxy:
    def __init__(self, summaries):
        self.controls, self.summaries = [], list(summaries)

    def control(self, body):
        self.controls.append(body)
        return {"window_id": "w7"} if body["action"] == "measure" else {}

    def stats(self, window_id=None):
        assert window_id == "w7"
        return self.summaries.pop(0) if len(self.summaries) > 1 else self.summaries[0]


SETTINGS = WorkloadSettings("/checkout", 10, 1.0, 15, 300, 297, 60, 1)


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
