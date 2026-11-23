"""Milestone 5 closeout fixes (decision 0018, closeout amendment). Offline."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from safeops.experiment.schema import RecordValidationError, validate_record_json
from tests.conftest import FixedProvenance
from tests.test_m5_records import (
    UNIT,
    Fixed,
    Rec,
    _check,
    _records,
    _runner,
    _set,
)


@pytest.fixture
def good(tmp_path) -> dict:
    _runner(tmp_path / "good").run(UNIT)
    (rec,) = _records(tmp_path / "good")
    return rec


# --- 1. timestamps: no ordering rule; wall_seconds from the monotonic clock --------------------


def _clocks(r, wall_offsets: list[float], mono: list[float]):
    """Make the runner's and writer's wall clocks and the runner's monotonic clock scripted."""
    from tests.conftest import FIXED_TIME

    walls = iter(FIXED_TIME + timedelta(seconds=s) for s in wall_offsets)
    monos = iter(mono)
    r._wall_clock = lambda: next(walls)
    r.records._clock = lambda: next(walls)
    r._monotonic = lambda: next(monos)


def test_a_backwards_wall_clock_step_keeps_the_record_and_its_monotonic_duration(tmp_path) -> None:
    r = _runner(tmp_path)
    # started_at at +0 s; the runtime context's `now` at -3600 s; finalized_at at -3500 s.
    _clocks(r, [0, -3600, -3500], [1000.0, 1123.5])
    r.run(UNIT)
    (rec,) = _records(tmp_path)  # valid, in the dataset, not rejected
    assert rec["finalized_at"] < rec["started_at"]  # the step is visible from the timestamps
    assert rec["totals"]["wall_seconds"] == 123.5  # elapsed real time, not -3500 s, not 0


def test_a_forward_wall_clock_jump_does_not_inflate_wall_seconds(tmp_path) -> None:
    r = _runner(tmp_path)
    _clocks(r, [0, 7200, 7300], [50.0, 92.25])
    r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["totals"]["wall_seconds"] == 42.25


def test_a_negative_monotonic_duration_fails_loudly(tmp_path) -> None:
    r = _runner(tmp_path)
    _clocks(r, [0, 10, 20], [500.0, 499.0])
    with pytest.raises(ValueError, match="monotonic elapsed time is negative"):
        r.run(UNIT)
    assert _records(tmp_path) == []


def test_raw_monotonic_samples_never_reach_the_record(tmp_path) -> None:
    from safeops.experiment.records import RECORD_FIELDS

    r = _runner(tmp_path)
    _clocks(r, [0, 10, 20], [987654.321, 987660.0])
    r.run(UNIT)
    (path,) = (tmp_path / "runs").glob("*.json")
    text = path.read_text()
    assert "987654" not in text and "987660" not in text and "monotonic" not in text
    assert not any("monotonic" in f for f in RECORD_FIELDS)


def test_wall_seconds_is_not_checked_against_wall_clock_subtraction(good) -> None:
    _check(_set(good, ("totals", "wall_seconds"), 12345.0))
    _check(_set(good, ("finalized_at",), "2000-01-01T00:00:00Z"))  # ordering is unconstrained


# --- 2. strict JSON: NaN, Infinity, -Infinity are refused before validation ---------------------


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("where", ["trajectory_result", "model_parameters"])
def test_non_standard_json_constants_are_refused_in_any_payloads(good, constant, where) -> None:
    marker = '"__CONSTANT__"'
    if where == "trajectory_result":
        rec = _set(good, ("trajectory", 0, "result"), {"cpu": "__CONSTANT__"})
    else:
        rec = _set(good, ("provenance", "model_parameters"), {"temperature": "__CONSTANT__"})
    text = json.dumps(rec).replace(marker, constant)
    assert constant in text and marker not in text
    with pytest.raises(RecordValidationError, match="non-standard JSON constant"):
        validate_record_json(text)


# --- 3. SteadyState.state shape -------------------------------------------------------------


def _complete(good) -> dict:
    return good["steady_state"]


BAD_COMPLETE = {
    # Self-consistent otherwise (150 of 150 scheduled succeeded, rate over size), so only
    # the "complete means all of size scheduled" rule can reject it.
    "complete with scheduled < size": {"scheduled": 150, "succeeded": 150, "success_rate": 0.5},
    "complete without succeeded": {"succeeded": None, "success_rate": None},
    "complete without success_rate": {"success_rate": None},
    "complete with a failure": {"failure": "lost"},
}


@pytest.mark.parametrize("changes", BAD_COMPLETE.values(), ids=BAD_COMPLETE.keys())
def test_a_complete_window_has_its_full_shape(good, changes) -> None:
    with pytest.raises(RecordValidationError, match="steady_state"):
        _check(_set(good, ("steady_state",), {**_complete(good), **changes}))


def _unmeasured(state: str) -> dict:
    return {
        "window_id": "w1",
        "state": state,
        "size": 300,
        "scheduled": 120,
        "succeeded": None,
        "min_successes": 297,
        "success_rate": None,
        "p95_latency_ms": None,
        "failure": "generator lost",
    }


BAD_UNMEASURED = {
    "failed with a success rate": ("failed", {"succeeded": 100, "success_rate": 100 / 300}),
    "failed with p95": ("failed", {"p95_latency_ms": 30.0}),
    "failed without a reason": ("failed", {"failure": None}),
    "timeout with success metrics": ("timeout", {"succeeded": 100, "success_rate": 100 / 300}),
}


@pytest.fixture
def no_baseline(tmp_path) -> dict:
    """A real no_steady_state discard record from a failed window."""
    from safeops.environment.workload import SteadyState, SteadyStateMeasurementFailed

    class Lost:
        def start_workload(self):
            pass

        def verify_steady_state(self):
            return SteadyState("w1", "failed", 300, 120, None, 297, None, None, "generator lost")

        def stop_workload(self):
            pass

    r = _runner(tmp_path / "nb", workload=Lost())
    with pytest.raises(SteadyStateMeasurementFailed):
        r.run(UNIT)
    (rec,) = _records(tmp_path / "nb")
    return rec


def test_a_real_unmeasured_window_validates(no_baseline) -> None:
    _check(no_baseline)
    assert no_baseline["steady_state"]["succeeded"] is None


@pytest.mark.parametrize("state,changes", BAD_UNMEASURED.values(), ids=BAD_UNMEASURED.keys())
def test_an_unmeasured_window_carries_no_metrics_inside_a_discard(
    no_baseline, state, changes
) -> None:
    rec = _set(no_baseline, ("steady_state",), {**_unmeasured(state), **changes})
    with pytest.raises(RecordValidationError, match="steady_state"):
        _check(rec)


@pytest.mark.parametrize("changes", BAD_COMPLETE.values(), ids=BAD_COMPLETE.keys())
def test_a_malformed_complete_window_is_rejected_inside_a_discard(tmp_path, changes) -> None:
    from safeops.environment.workload import SteadyStateNotReached
    from tests.test_m4_faults import _LoadRecorder

    r = _runner(tmp_path / "ns", workload=_LoadRecorder(steady_ok=False))
    with pytest.raises(SteadyStateNotReached):
        r.run(UNIT)
    (rec,) = _records(tmp_path / "ns")
    _check(rec)
    with pytest.raises(RecordValidationError, match="steady_state"):
        _check(_set(rec, ("steady_state",), {**rec["steady_state"], **changes}))


# --- 4. started_at is required, never fabricated -------------------------------------------


def test_the_writer_refuses_a_missing_started_at(tmp_path) -> None:
    from safeops.experiment.records import RunRecordWriter

    w = RunRecordWriter(runs_dir=tmp_path, provenance=FixedProvenance())
    base = dict(
        run_id="6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b",
        unit=UNIT,
        provenance=FixedProvenance().collect(),
        wall_seconds=1.0,
    )
    with pytest.raises(TypeError, match="requires started_at captured at run start"):
        w.finalize(**base)
    with pytest.raises(TypeError, match="requires started_at captured at run start"):
        w.finalize(**base, started_at=None)
    assert not list(tmp_path.glob("*.json"))


def test_a_supplied_started_at_is_kept_exactly(tmp_path) -> None:
    r = _runner(tmp_path)
    _clocks(r, [0, 10, 20], [0.0, 20.0])
    r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["started_at"] == "2026-11-19T18:00:00Z"


# --- 5. a measured harmful on a completed run carries its evidence -------------------------------


HARM = {
    "harmful true, violations null": (True, None, False),
    "harmful false, violations null": (False, None, False),
    "harmful true, no violations": (True, [], False),
    "harmful false, a violation": (False, [{"rule": "replicas_safe_range"}], False),
    "harmful true, a violation": (True, [{"rule": "replicas_safe_range"}], True),
    "harmful false, no violations": (False, [], True),
}


@pytest.mark.parametrize("harmful,violations,valid", HARM.values(), ids=HARM.keys())
def test_harm_verdict_and_evidence(good, harmful, violations, valid) -> None:
    rec = _set(good, ("outcome", "harmful"), harmful)
    rec = _set(rec, ("outcome", "harm_violations"), violations)
    category = {(True, False): "correct_autonomy", (True, True): "harmful_but_effective"}
    rec = _set(rec, ("outcome", "category"), category[(rec["outcome"]["effective"], harmful)])
    if valid:
        _check(rec)
    else:
        with pytest.raises(RecordValidationError):
            _check(rec)


def test_a_discarded_run_is_not_over_constrained(tmp_path) -> None:
    """Discards have no outcome at all; the harm-evidence rule doesn't reach them."""
    from safeops.experiment.runner import PostAgentDiscard

    r = _runner(tmp_path, harm=Fixed(raises=KeyError("x")))
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["outcome"] is None
    _check(rec)


def test_the_runner_path_still_writes_harm_evidence(tmp_path) -> None:
    r = _runner(tmp_path, harm=Fixed(Rec({"harmful": True, "harm_violations": [{"rule": "x"}]})))
    r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["outcome"]["harmful"] is True and rec["outcome"]["harm_violations"]


# --- final cleanup: numbers that overflow to infinity while decoding ---------------------------


@pytest.mark.parametrize("number", ["1e400", "-1e400", "1e309"])
@pytest.mark.parametrize("where", ["trajectory_result", "model_parameters"])
def test_overflowing_numbers_in_any_payloads_are_refused(good, number, where) -> None:
    """parse_constant never sees ordinary numbers; 1e309 looks like an ordinary value but
    decodes to inf. It must be refused before validation, not coerced or dropped."""
    marker = '"__NUMBER__"'
    if where == "trajectory_result":
        rec = _set(good, ("trajectory", 0, "result"), {"metrics": [{"x": "__NUMBER__"}]})
    else:
        rec = _set(good, ("provenance", "model_parameters"), {"nested": {"x": "__NUMBER__"}})
    text = json.dumps(rec).replace(marker, number)
    assert number in text and json.loads(text) is not None  # Python's parser accepts it
    with pytest.raises(RecordValidationError, match="non-finite number at"):
        validate_record_json(text)


def test_finite_numbers_in_any_payloads_still_validate(good) -> None:
    # Version 2 validates stored tool results (decision 0023), so the finite numbers ride
    # in a real get_metrics result, with its canonical digest.
    from safeops.tools.canonical import result_digest

    values = [1e308, -2.5, 0.0, 2.0]
    result = {
        "status": "ok",
        "data": {
            "service": "orders",
            "metric": "request_rate",
            "window": 1,
            "unit": "requests_per_second",
            "start": "2026-11-19T18:00:00Z",
            "end": "2026-11-19T18:00:15Z",
            "step_s": 5,
            "range_s": 20,
            "points": [
                {"t": f"2026-11-19T18:00:{5 * i:02d}Z", "v": v} for i, v in enumerate(values)
            ],
            "point_count": 4,
            "truncated": False,
        },
    }
    rec = _set(good, ("trajectory", 0, "tool"), "get_metrics")
    rec = _set(
        rec, ("trajectory", 0, "args"), {"service": "orders", "metric": "request_rate", "window": 1}
    )
    rec = _set(rec, ("trajectory", 0, "result"), result)
    rec = _set(rec, ("trajectory", 0, "result_digest"), result_digest(result))
    rec = _set(rec, ("provenance", "model_parameters"), {"x": 1e308, "z": 0.0, "n": [1, 2.0]})
    validate_record_json(json.dumps(rec))
