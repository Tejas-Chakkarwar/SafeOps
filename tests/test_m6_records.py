"""Milestone 6: run record version 2, the two clock-probe stages, and the probe itself
(decisions 0017 §6, §7 and 0023). Offline, with the real runner, writer and validator and
fabricated components (Rule 17).
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from safeops.enums import RunStage
from safeops.environment import clock
from safeops.environment.clock import ClockProbeError, ClockReading, take_reading
from safeops.experiment.records import RECORD_FIELDS
from safeops.experiment.runner import PostAgentDiscard, PreRunDiscard
from safeops.experiment.schema import (
    CAUSE_STAGE,
    CLOCK_STAGE_CAUSES,
    CLOCK_STAGES,
    POST_AGENT_STAGES,
    PRE_AGENT_STAGES,
    SCHEMA_VERSION,
    SCHEMA_VERSIONS,
    STAGE_PRODUCES,
    STAGE_PRODUCES_V1,
    RecordValidationError,
    RunRecordV1,
    RunRecordV2,
    stage_phase,
    validate_record_json,
)
from tests.conftest import FIXED_TIME, clock_reading
from tests.test_m5_records import UNIT, _as_discard, _check, _records, _runner, _set, _strip_agent

V1_DIR = Path(__file__).resolve().parent / "fixtures" / "records_v1"
V1_FILES = sorted(V1_DIR.glob("*.json"))
CLOCK_FIELDS = ("clock_before_load", "clock_after_measurements", "clock_change")
S = RunStage


@pytest.fixture
def good(tmp_path) -> dict:
    _runner(tmp_path / "good").run(UNIT)
    (rec,) = _records(tmp_path / "good")
    return rec


# --- version compatibility -------------------------------------------------------------------


def test_the_frozen_v1_fixtures_are_real_records_of_every_kind() -> None:
    kinds = {json.loads(p.read_text())["discarded"] for p in V1_FILES}
    assert len(V1_FILES) == 3 and kinds == {True, False}


@pytest.mark.parametrize("path", V1_FILES, ids=lambda p: p.stem)
def test_historical_v1_records_still_validate_as_v1(path) -> None:
    rec = validate_record_json(path.read_text())
    assert type(rec) is RunRecordV1 and rec.schema_version == 1
    assert not set(CLOCK_FIELDS) & set(json.loads(path.read_text()))


@pytest.mark.parametrize("path", V1_FILES, ids=lambda p: p.stem)
@pytest.mark.parametrize("field", CLOCK_FIELDS)
def test_v1_refuses_every_v2_clock_field(path, field) -> None:
    rec = json.loads(path.read_text())
    for value in (None, clock_reading().to_record()):
        with pytest.raises(RecordValidationError, match="Extra inputs are not permitted"):
            validate_record_json(json.dumps({**rec, field: value}))


@pytest.mark.parametrize("stage", CLOCK_STAGES)
def test_v1_refuses_the_v2_stages(stage) -> None:
    rec = json.loads((V1_DIR / "injection_failed.json").read_text())
    rec["discard_reason"]["stage"] = stage
    rec["discard_reason"]["cause"] = "infrastructure_failure"
    rec["discard_reason"]["phase"] = stage_phase(stage)
    with pytest.raises(RecordValidationError, match="does not exist in schema version 1"):
        validate_record_json(json.dumps(rec))


def test_v1_s_stage_table_is_unchanged() -> None:
    assert list(STAGE_PRODUCES_V1) == [s for s in RunStage if s not in CLOCK_STAGES]
    assert {s: STAGE_PRODUCES[s] for s in STAGE_PRODUCES_V1} == STAGE_PRODUCES_V1


def test_new_records_are_v2_and_dispatch_picks_the_version(good) -> None:
    assert good["schema_version"] == SCHEMA_VERSION == 2
    assert type(validate_record_json(json.dumps(good))) is RunRecordV2
    assert SCHEMA_VERSIONS == {1: RunRecordV1, 2: RunRecordV2}
    assert issubclass(RunRecordV2, RunRecordV1)  # every version-1 rule is inherited
    assert {"clock_before_load", "clock_after_measurements"} <= RECORD_FIELDS
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Input should be 2"):
        RunRecordV2.model_validate_json(json.dumps({**good, "schema_version": 1}))
    no_clocks = {k: v for k, v in good.items() if k not in CLOCK_FIELDS}
    with pytest.raises(RecordValidationError, match="Field required"):
        validate_record_json(json.dumps(no_clocks))  # a v2 record has every key, even null
    with pytest.raises(RecordValidationError, match="Extra inputs"):
        validate_record_json(json.dumps({**good, "schema_version": 1}))  # claims 1, carries 2


@pytest.mark.parametrize("version", [0, 3, "2", 2.0, True, None])
def test_unknown_versions_fail_closed(good, version) -> None:
    with pytest.raises(RecordValidationError, match="unknown schema_version"):
        validate_record_json(json.dumps({**good, "schema_version": version}))


# --- the stages ------------------------------------------------------------------------------


def test_the_clock_stages_are_placed_and_partitioned_exactly() -> None:
    order = list(RunStage)
    assert order.index(S.CLOCK_BEFORE_LOAD) == order.index(S.PRE_RUN_CLEAN_STATE) + 1
    assert order.index(S.CLOCK_BEFORE_LOAD) + 1 == order.index(S.START_WORKLOAD)
    assert order.index(S.CLOCK_AFTER_MEASUREMENTS) == order.index(S.HARM) + 1 == len(order) - 1
    assert S.CLOCK_BEFORE_LOAD in PRE_AGENT_STAGES and S.CLOCK_BEFORE_LOAD not in POST_AGENT_STAGES
    assert S.CLOCK_AFTER_MEASUREMENTS in POST_AGENT_STAGES
    assert S.CLOCK_AFTER_MEASUREMENTS not in PRE_AGENT_STAGES
    assert PRE_AGENT_STAGES + POST_AGENT_STAGES == tuple(RunStage) == tuple(STAGE_PRODUCES)
    assert stage_phase(S.CLOCK_BEFORE_LOAD) == "pre_agent"
    assert stage_phase(S.CLOCK_AFTER_MEASUREMENTS) == "post_agent"


def test_each_reading_is_produced_by_its_own_stage() -> None:
    assert STAGE_PRODUCES[S.CLOCK_BEFORE_LOAD] == ("clock_before_load",)
    assert STAGE_PRODUCES[S.CLOCK_AFTER_MEASUREMENTS] == (
        "clock_after_measurements",
        "clock_change",
    )
    assert STAGE_PRODUCES[S.START_WORKLOAD] == () and STAGE_PRODUCES[S.HARM] == ()


def test_clock_probe_failures_have_explicit_causes() -> None:
    assert CLOCK_STAGE_CAUSES == {
        S.CLOCK_BEFORE_LOAD: {"infrastructure_failure"},
        S.CLOCK_AFTER_MEASUREMENTS: {"infrastructure_failure", "operator_abort"},
    }
    assert not set(CAUSE_STAGE.values()) & set(CLOCK_STAGES)


# --- runner: each probe names its stage before it runs ---------------------------------------


class ProbeFails:
    """read_clock that fails on call number `n` (1 = before the load, 2 = after)."""

    def __init__(self, env, n: int) -> None:
        self.env, self.n, self.calls = env, n, 0

    def __call__(self):
        self.calls += 1
        self.env.calls.append("read_clock")
        if self.calls == self.n:
            raise ClockProbeError("clock probe could not be created: API server unavailable")
        return clock_reading(offset_s=0)


def test_a_first_probe_failure_is_attributed_to_its_own_stage(tmp_path) -> None:
    r = _runner(tmp_path)
    started = []
    r.workload.start_workload = lambda: started.append(True)
    r.environment.read_clock = ProbeFails(r.environment, 1)
    with pytest.raises(ClockProbeError):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["discard_reason"]["stage"] == "clock_before_load"  # not pre_run_clean_state
    assert rec["discard_reason"]["cause"] == "infrastructure_failure"
    assert rec["discard_reason"]["phase"] == "pre_agent"
    assert rec["clean_state_verified"] is True and rec["reset_verified"] is None
    assert [rec[f] for f in CLOCK_FIELDS] == [None, None, None] and rec["steady_state"] is None
    assert started == []  # the load never began


def test_a_second_probe_failure_is_attributed_to_its_own_stage(tmp_path) -> None:
    r = _runner(tmp_path)
    r.environment.read_clock = ProbeFails(r.environment, 2)
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["discard_reason"]["stage"] == "clock_after_measurements"  # not harm
    assert rec["discard_reason"]["phase"] == "post_agent"
    assert rec["clock_before_load"] is not None
    assert rec["clock_after_measurements"] is None and rec["clock_change"] is None
    assert rec["postaction_snapshot_hash"] is not None and rec["outcome"] is None
    calls = r.environment.calls
    # harm measured first (the post-action snapshot), the probe, then the cleanup reset
    assert calls.index("read_clock", calls.index("wait_stabilization_window")) < len(calls) - 2
    assert calls[-2:] == ["reset_environment", "verify_clean_state"]


def test_the_probes_bracket_the_load_and_precede_the_destructive_cleanup(tmp_path) -> None:
    r = _runner(tmp_path)
    log = r.environment.calls
    r.workload.start_workload = lambda: log.append("start_workload")
    r.workload.stop_workload = lambda: log.append("stop_workload")
    r.run(UNIT)
    first, second = [i for i, c in enumerate(log) if c == "read_clock"]
    assert log[first - 1] == "verify_clean_state" and log[first + 1] == "start_workload"
    assert log[second - 1] == "capture_snapshot"  # the post-action snapshot: measurements done
    assert log[second + 1 :] == ["stop_workload", "reset_environment", "verify_clean_state"]


# --- stage-governed presence, from real runs ------------------------------------------------


def _crash(r, stage: str) -> None:
    def boom(*a, **k):
        raise RuntimeError(f"{stage} crashed")

    target = {
        "start_workload": (r.workload, "start_workload"),
        "steady_state": (r.workload, "verify_steady_state"),
        "agent": (r.agent, "investigate"),
        "harm": (r.harm, "check"),
    }[stage]
    setattr(*target, boom)


@pytest.mark.parametrize(
    ("stage", "first", "second"),
    [
        ("start_workload", True, False),
        ("steady_state", True, False),
        ("agent", True, False),
        ("harm", True, False),
    ],
)
def test_runner_records_carry_exactly_the_readings_their_stage_produced(
    tmp_path, stage, first, second
) -> None:
    r = _runner(tmp_path)
    _crash(r, stage)
    with pytest.raises((RuntimeError, PostAgentDiscard)):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["discard_reason"]["stage"] == stage
    assert (rec["clock_before_load"] is not None, rec["clock_after_measurements"] is not None) == (
        first,
        second,
    )
    assert rec["clock_change"] is None


def test_a_pre_run_dirty_discard_has_neither_reading(tmp_path) -> None:
    r = _runner(tmp_path)
    r.environment.clean = False
    r.environment.quarantine_and_rebuild = lambda reason="": None
    with pytest.raises(PreRunDiscard):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert [rec[f] for f in CLOCK_FIELDS] == [None, None, None]
    assert "read_clock" not in r.environment.calls


def test_a_completed_run_has_both_readings_and_their_derived_change(good) -> None:
    before, after = good["clock_before_load"], good["clock_after_measurements"]
    assert before == after == clock_reading().to_record()
    assert good["clock_change"] == {"change_s": 0.0, "interpretation": "no_change_detected"}


# --- stage-governed presence, as validation ------------------------------------------------------


def _discard_at(good: dict, stage: str) -> dict:
    """A consistent discard at `stage`: its own and later stages' data removed."""
    rec = _strip_agent(good) if stage_phase(RunStage(stage)) == "pre_agent" else good
    order = list(STAGE_PRODUCES)
    i = order.index(RunStage(stage))
    for later in order[i:]:
        for name in STAGE_PRODUCES[later]:
            rec = _set(rec, (name,), None)
    return _as_discard(rec, stage)


@pytest.mark.parametrize(
    ("stage", "first", "second"),
    [
        ("clock_before_load", False, False),
        ("start_workload", True, False),
        ("steady_state", True, False),
        ("runtime_context", True, False),
        ("stabilization", True, False),
        ("harm", True, False),
        ("clock_after_measurements", True, False),
    ],
)
def test_discard_records_are_valid_with_exactly_their_readings(good, stage, first, second) -> None:
    rec = _discard_at(good, stage)
    assert (rec["clock_before_load"] is not None, rec["clock_after_measurements"] is not None) == (
        first,
        second,
    )
    _check(rec)


def test_a_reading_its_stage_could_not_have_produced_is_rejected(good) -> None:
    reading = clock_reading().to_record()
    with pytest.raises(
        RecordValidationError, match="discarded at clock_before_load, yet carries clock_before_load"
    ):
        _check(_set(_discard_at(good, "clock_before_load"), ("clock_before_load",), reading))
    for stage in ("start_workload", "harm", "clock_after_measurements"):
        rec = _set(_discard_at(good, stage), ("clock_after_measurements",), reading)
        with pytest.raises(
            RecordValidationError, match=f"discarded at {stage}, yet carries clock_after"
        ):
            _check(rec)
    dirty = _discard_at(good, "clock_before_load")
    dirty = _set(dirty, ("clean_state_verified",), False)
    dirty = _set(dirty, ("discard_reason", "stage"), "pre_run_clean_state")
    dirty = _set(dirty, ("discard_reason", "cause"), "pre_run_dirty")
    _check(dirty)
    with pytest.raises(RecordValidationError, match="yet carries clock_before_load"):
        _check(_set(dirty, ("clock_before_load",), reading))


def test_a_reading_already_produced_must_not_disappear(good) -> None:
    for stage in ("start_workload", "steady_state", "agent", "harm", "clock_after_measurements"):
        rec = _set(_discard_at(good, stage), ("clock_before_load",), None)
        with pytest.raises(
            RecordValidationError,
            match="clock_before_load from earlier stage clock_before_load is missing",
        ):
            _check(rec)
    for field in CLOCK_FIELDS:
        with pytest.raises(RecordValidationError, match="missing|clock_change"):
            _check(_set(good, (field,), None))


def test_a_one_reading_record_cannot_carry_a_two_reading_change(good) -> None:
    rec = _set(_discard_at(good, "harm"), ("clock_change",), good["clock_change"])
    with pytest.raises(RecordValidationError, match="clock_change needs both clock readings"):
        _check(rec)


@pytest.mark.parametrize(
    ("stage", "cause", "valid"),
    [
        ("clock_before_load", "infrastructure_failure", True),
        ("clock_after_measurements", "infrastructure_failure", True),
        ("clock_after_measurements", "operator_abort", True),
        ("clock_after_measurements", "not_implemented", False),
        ("clock_before_load", "operator_abort", False),
        ("clock_before_load", "pre_run_dirty", False),
        ("clock_before_load", "no_steady_state", False),
        ("clock_before_load", "workload_generator_busy", False),
        ("clock_after_measurements", "injection_failed", False),
        ("clock_after_measurements", "provider_failure", False),
    ],
)
def test_cause_and_clock_stage_must_agree(good, stage, cause, valid) -> None:
    rec = _set(_discard_at(good, stage), ("discard_reason", "cause"), cause)
    if valid:
        _check(rec)
    else:
        with pytest.raises(RecordValidationError):
            _check(rec)


# --- the clock evidence ------------------------------------------------------------------------


def _readings(offset2_s: int, latency_ms: int = 40) -> tuple[ClockReading, ClockReading]:
    at = FIXED_TIME
    first = ClockReading(at + timedelta(milliseconds=500), at + timedelta(milliseconds=540), at)
    later = at + timedelta(minutes=5)
    second = ClockReading(
        later + timedelta(milliseconds=500),
        later + timedelta(milliseconds=500 + latency_ms),
        later + timedelta(seconds=offset2_s),
    )
    return first, second


@pytest.mark.parametrize(
    ("offset2", "change", "meaning"),
    [
        (0, 0.0, "no_change_detected"),
        (1, 1.0, "no_change_detected"),  # within one second of resolution plus latency
        (-1, -1.0, "no_change_detected"),
        (2, 2.0, "change_detected"),
        (-2, -2.0, "change_detected"),
        (120, 120.0, "change_detected"),  # a sleep/resume jump
    ],
)
def test_the_change_and_its_interpretation_are_computed_by_hand(offset2, change, meaning) -> None:
    first, second = _readings(offset2)
    assert first.to_record()["offset_s"] == -0.52  # server minus the request's midpoint
    assert clock.clock_change(first, second) == {"change_s": change, "interpretation": meaning}


def test_a_slow_request_widens_the_uncertainty() -> None:
    assert clock.clock_change(*_readings(2, latency_ms=40))["interpretation"] == "change_detected"
    assert (
        clock.clock_change(*_readings(2, latency_ms=1500))["interpretation"] == "no_change_detected"
    )


@pytest.fixture
def jumped(tmp_path) -> dict:
    r = _runner(tmp_path)
    readings = iter([clock_reading(0), clock_reading(30, at=FIXED_TIME + timedelta(minutes=4))])
    r.environment.read_clock = lambda: next(readings)
    r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["clock_change"] == {"change_s": 30.0, "interpretation": "change_detected"}
    return rec


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (("clock_before_load", "host_before"), "2026-11-19T18:00:00.100000Z", "offset_s"),
        (("clock_after_measurements", "server_created"), "2026-11-19T18:04:31Z", "offset_s"),
        (("clock_before_load", "offset_s"), -0.53, "offset_s"),
        (("clock_change", "change_s"), 29.0, "change_s"),
        (("clock_change", "interpretation"), "no_change_detected", "interpretation"),
        (("clock_before_load", "server_created"), "2026-11-19T18:00:00.250000Z", "whole-second"),
    ],
)
def test_clock_derived_values_must_agree_with_the_readings(jumped, path, value, match) -> None:
    with pytest.raises(RecordValidationError, match=match):
        _check(_set(jumped, path, value))


def test_changing_a_reading_and_its_offset_still_disagrees_with_the_change(jumped) -> None:
    rec = _set(jumped, ("clock_after_measurements", "server_created"), "2026-11-19T18:04:00Z")
    rec = _set(rec, ("clock_after_measurements", "offset_s"), -0.52)  # offset re-sealed
    with pytest.raises(RecordValidationError, match="change_s"):
        _check(rec)


def test_a_completed_run_must_have_both_readings(good) -> None:
    with pytest.raises(RecordValidationError, match="clock_after_measurements"):
        _check(_set(_set(good, ("clock_after_measurements",), None), ("clock_change",), None))


# --- every applicable version-1 rule fires on version 2 -------------------------------------------


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (
            lambda r: _set(
                _as_discard(r, "inject", "injection_failed"), ("discard_reason", "stage"), "alert"
            ),
            "comes only from stage",
        ),
        (
            lambda r: _set(
                _discard_at(r, "clock_before_load"), ("discard_reason", "cause"), "pre_run_dirty"
            ),
            "pre_run_dirty needs",
        ),
        (
            lambda r: _set(r, ("execution", "steps", 0, "operation"), "scale_deployment"),
            "the proposal was rollback_deployment",
        ),
        (lambda r: _set(r, ("in_cohort_g",), False), "in_cohort_g"),
        (lambda r: _set(r, ("outcome", "harmful"), True), "harmful must be true exactly"),
        (lambda r: _set(r, ("alert",), None), "earlier stage"),
        (
            lambda r: _set(r, ("trajectory", 0, "result_digest"), "sha256:" + "0" * 64),
            "result_digest is not the digest",
        ),
        (lambda r: _set(r, ("steady_state", "success_rate"), 0.5), "success_rate"),
        (lambda r: _set(r, ("totals", "tokens_in"), 1), "totals.tokens_in"),
    ],
    ids=[
        "cause-stage",
        "pre-run-dirty",
        "proposal-execution",
        "cohort-g",
        "harm-evidence",
        "stage-presence",
        "trajectory-digest",
        "steady-state",
        "token-totals",
    ],
)
def test_version_1_rules_fire_on_version_2(good, change, match) -> None:
    _check(good)
    with pytest.raises(RecordValidationError, match=match):
        _check(change(copy.deepcopy(good)))


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e400"])
def test_non_finite_numbers_are_refused_in_version_2(good, number) -> None:
    text = json.dumps(_set(good, ("clock_before_load", "offset_s"), "__N__")).replace(
        '"__N__"', number
    )
    with pytest.raises(RecordValidationError, match="not standard JSON"):
        validate_record_json(text)


# --- the probe ---------------------------------------------------------------------------------


class ProbeKubectl:
    """A runtime client double for take_reading."""

    def __init__(
        self,
        *,
        created="2026-11-22T12:00:00Z",
        create_error=None,
        delete_error=None,
        left=False,
        rename=False,
    ) -> None:
        self.created, self.create_error, self.delete_error = created, create_error, delete_error
        self.left, self.rename = left, rename
        self.calls: list[tuple] = []

    def create_returning(self, obj):
        self.calls.append(("create", obj["metadata"]["name"], obj["metadata"]["namespace"]))
        self.obj = obj
        if self.create_error:
            raise self.create_error
        meta = dict(obj["metadata"], creationTimestamp=self.created)
        if self.rename:
            meta["name"] = "someone-else"
        return {**obj, "metadata": meta}

    def delete(self, resource, kind, name, namespace, timeout_s=180):
        self.calls.append(("delete", kind, name, namespace))
        if self.delete_error:
            raise self.delete_error

    def get_json(self, *args):
        self.calls.append(("get", *args))
        return {"items": [{"metadata": {"name": args[-1]}}] if self.left else []}


def _clock_ticks():
    ticks = iter(
        [
            datetime(2026, 11, 22, 12, 0, 0, 400000, tzinfo=UTC),
            datetime(2026, 11, 22, 12, 0, 0, 460000, tzinfo=UTC),
        ]
    )
    return lambda: next(ticks)


def test_a_reading_creates_reads_removes_and_verifies_a_fresh_probe() -> None:
    k = ProbeKubectl()
    reading = take_reading(k, "testbed", wall_clock=_clock_ticks(), new_id=lambda: "abc123")
    name = "safeops-clock-probe-abc123"
    assert k.calls == [
        ("create", name, "testbed"),
        ("delete", "ConfigMap", name, "testbed"),
        ("get", "configmaps", "-n", "testbed", "--field-selector", f"metadata.name={name}"),
    ]
    assert k.obj["kind"] == "ConfigMap" and "data" not in k.obj
    assert reading.to_record() == {
        "host_before": "2026-11-22T12:00:00.400000Z",
        "host_after": "2026-11-22T12:00:00.460000Z",
        "server_created": "2026-11-22T12:00:00Z",
        "offset_s": -0.43,
    }


def test_each_reading_uses_a_new_probe() -> None:
    k = ProbeKubectl()
    take_reading(k, "testbed")
    take_reading(k, "testbed")
    created = [c[1] for c in k.calls if c[0] == "create"]
    assert len(set(created)) == 2 and all(n.startswith(clock.PROBE_PREFIX) for n in created)


@pytest.mark.parametrize(
    ("double", "match"),
    [
        (ProbeKubectl(create_error=RuntimeError("Forbidden")), "could not create"),
        (ProbeKubectl(created=None), "creationTimestamp"),
        (ProbeKubectl(created="2026-11-22T12:00:00.5Z"), "creationTimestamp"),
        (ProbeKubectl(created="2026-11-22T12:00:00+02:00"), "creationTimestamp"),
        (ProbeKubectl(rename=True), "creationTimestamp"),
        (ProbeKubectl(delete_error=RuntimeError("timed out")), "could not remove"),
        (ProbeKubectl(left=True), "could not remove"),
    ],
)
def test_a_probe_that_cannot_be_taken_or_removed_fails_clearly(double, match) -> None:
    with pytest.raises(ClockProbeError, match=match):
        take_reading(double, "testbed")
    if match != "could not remove":  # a failed reading still tries to remove its probe
        assert any(c[0] == "delete" for c in double.calls)


def test_the_probe_goes_through_the_guarded_runtime_path() -> None:
    import dataclasses

    from safeops.cluster import GuardError, Kubectl, Phase, load_cluster_config

    cfg = load_cluster_config()
    k = Kubectl(
        dataclasses.replace(cfg),
        Phase.RUNTIME,
        runner=None,
        kubeconfig=Path("/x"),
        verify_identity=False,
    )
    k._cluster_scoped = frozenset()
    with pytest.raises(ClockProbeError, match="GuardError"):
        take_reading(k, "kube-system")  # the runtime guard allows the testbed only
    with pytest.raises(GuardError):
        k.create_returning(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "x", "namespace": "default"},
            }
        )


def test_a_failure_before_the_load_was_requested_stops_nothing(tmp_path) -> None:
    """Milestone 6 review: a failed first reading must not stop a load this run never asked
    for: the generator may be running someone else's."""
    r = _runner(tmp_path)
    stopped = []
    r.workload.stop_workload = lambda: stopped.append(True)
    r.environment.read_clock = ProbeFails(r.environment, 1)
    with pytest.raises(ClockProbeError):
        r.run(UNIT)
    assert stopped == []


def test_a_failure_once_the_load_was_requested_still_stops_it(tmp_path) -> None:
    r = _runner(tmp_path)
    stopped = []
    r.workload.stop_workload = lambda: stopped.append(True)

    def lost():
        raise RuntimeError("the start's response was lost; it may be running")

    r.workload.start_workload = lost
    with pytest.raises(RuntimeError):
        r.run(UNIT)
    assert stopped == [True]
