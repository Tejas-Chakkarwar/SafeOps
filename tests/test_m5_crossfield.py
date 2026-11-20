"""Milestone 5 cross-field review: derived fields must agree with the evidence beside them
(decision 0018, cross-field amendment). Offline, fabricated records."""

from __future__ import annotations

import copy
import json

import pytest

from safeops.enums import RunStage
from safeops.experiment.records import load_record
from safeops.experiment.runner import PreRunDiscard
from safeops.experiment.schema import CAUSE_STAGE, RecordValidationError
from tests.test_m5_records import (
    UNIT,
    Fixed,
    _as_discard,
    _check,
    _records,
    _runner,
    _set,
)

PATCH_POOL = {
    "type": "patch_configmap",
    "target": {"kind": "ConfigMap", "ns": "testbed", "name": "db-pool-config"},
    "params": {"max_pool_size": 20},
}


@pytest.fixture
def good(tmp_path) -> dict:
    _runner(tmp_path / "good").run(UNIT)
    (rec,) = _records(tmp_path / "good")
    return rec


def _step(op, status, error=None):
    return {"operation": op, "status": status, "error": error}


def _exec(rec: dict, status: str, steps, error=None, action=None) -> dict:
    rec = _set(rec, ("execution",), {"status": status, "error": error, "steps": steps})
    if action is not None:
        rec = _set(rec, ("proposal", "recommended_action"), action)
    in_g = status == "applied"
    rec = _set(rec, ("in_cohort_g",), in_g)
    return _set(rec, ("outcome", "category"), rec["outcome"]["category"] if in_g else None)


# --- 1. execution status <-> steps <-> proposed action ---------------------------------------


BAD_EXECUTIONS = {
    "applied with a failed step": (
        "applied",
        [_step("rollback_deployment", "failed", "x")],
        None,
        None,
    ),
    "applied with no steps": ("applied", [], None, None),
    "applied with a top-level error": (
        "applied",
        [_step("rollback_deployment", "applied")],
        "x",
        None,
    ),
    "partially_applied all applied": (
        "partially_applied",
        [_step("patch_configmap", "applied"), _step("restart_deployment", "applied")],
        None,
        PATCH_POOL,
    ),
    "partially_applied all failed": (
        "partially_applied",
        [_step("patch_configmap", "failed", "x"), _step("restart_deployment", "skipped")],
        None,
        PATCH_POOL,
    ),
    "refused_by_envelope with an applied step": (
        "refused_by_envelope",
        [_step("rollback_deployment", "applied")],
        None,
        None,
    ),
    "proposed rollback, executor scaled": (
        "applied",
        [_step("scale_deployment", "applied")],
        None,
        None,
    ),
    "non-composite with two steps": (
        "applied",
        [_step("rollback_deployment", "applied"), _step("rollback_deployment", "applied")],
        None,
        None,
    ),
    "patch_configmap steps reversed": (
        "applied",
        [_step("restart_deployment", "applied"), _step("patch_configmap", "applied")],
        None,
        PATCH_POOL,
    ),
    "patch_configmap missing its restart": (
        "applied",
        [_step("patch_configmap", "applied")],
        None,
        PATCH_POOL,
    ),
    "patch_configmap rejected out of order": (
        "rejected_invalid_action",
        [_step("restart_deployment", "failed", "422")],
        None,
        PATCH_POOL,
    ),
}


@pytest.mark.parametrize(
    "status,steps,error,action", BAD_EXECUTIONS.values(), ids=BAD_EXECUTIONS.keys()
)
def test_execution_status_must_agree_with_its_steps(good, status, steps, error, action) -> None:
    with pytest.raises(RecordValidationError):
        _check(_exec(good, status, steps, error, action))


GOOD_EXECUTIONS = {
    "applied rollback": ("applied", [_step("rollback_deployment", "applied")], None, None),
    "applied composite": (
        "applied",
        [_step("patch_configmap", "applied"), _step("restart_deployment", "applied")],
        None,
        PATCH_POOL,
    ),
    "composite half-applied (§9.2)": (
        "partially_applied",
        [_step("patch_configmap", "applied"), _step("restart_deployment", "failed", "timeout")],
        None,
        PATCH_POOL,
    ),
    "refused before any write": ("refused_by_envelope", [], "replicas outside 0..20", None),
    "rejected by Kubernetes": (
        "rejected_invalid_action",
        [_step("rollback_deployment", "failed", "422")],
        "422",
        None,
    ),
    "rejected with no step (permissive)": ("rejected_invalid_action", [], "422", None),
    "composite rejected at its first write": (
        "rejected_invalid_action",
        [_step("patch_configmap", "failed", "422")],
        "422",
        PATCH_POOL,
    ),
}


@pytest.mark.parametrize(
    "status,steps,error,action", GOOD_EXECUTIONS.values(), ids=GOOD_EXECUTIONS.keys()
)
def test_consistent_executions_validate(good, status, steps, error, action) -> None:
    _check(_exec(good, status, steps, error, action))


# --- 2. discard cause <-> stage --------------------------------------------------------------


def test_the_cause_table_names_real_stages() -> None:
    assert set(CAUSE_STAGE.values()) <= set(RunStage)
    assert CAUSE_STAGE["provider_failure"] == RunStage.ROUTE_PROPOSAL


def _discard_records(tmp_path) -> dict[str, dict]:
    """One real runner-produced record per specific cause."""
    from safeops.environment.workload import WorkloadGeneratorBusy
    from safeops.faults.injector import FaultInjectionError, FaultVerificationFailed
    from tests.conftest import FakeInjector
    from tests.test_m4_faults import _LoadRecorder

    out = {}

    def run(name, r, exc_type):
        with pytest.raises(exc_type):
            r.run(UNIT)
        (out[name],) = _records(tmp_path / name)

    r = _runner(tmp_path / "pre_run_dirty")
    r.environment.clean = False
    r.environment.quarantine_and_rebuild = lambda reason="": None
    run("pre_run_dirty", r, PreRunDiscard)
    run(
        "workload_generator_busy",
        _runner(
            tmp_path / "workload_generator_busy",
            workload=_LoadRecorder(start_raises=WorkloadGeneratorBusy("x")),
        ),
        WorkloadGeneratorBusy,
    )
    from safeops.environment.workload import SteadyStateNotReached

    run(
        "no_steady_state",
        _runner(tmp_path / "no_steady_state", workload=_LoadRecorder(steady_ok=False)),
        SteadyStateNotReached,
    )
    inj = FakeInjector()
    inj.inject = lambda fault_id, fault_seed: (_ for _ in ()).throw(FaultInjectionError("x"))
    run(
        "injection_failed",
        _runner(tmp_path / "injection_failed", injector=inj),
        FaultInjectionError,
    )
    inj = FakeInjector()
    inj.verify_fault_present = lambda f, timeout_s=None: (_ for _ in ()).throw(
        FaultVerificationFailed("x")
    )
    run(
        "detection_unmeasurable",
        _runner(tmp_path / "detection_unmeasurable", injector=inj),
        FaultVerificationFailed,
    )
    from safeops.faults.injector import FaultNotPresent

    run(
        "silent_non_injection",
        _runner(tmp_path / "silent_non_injection", injector=FakeInjector(present=False)),
        FaultNotPresent,
    )
    return out


def test_each_specific_cause_validates_at_its_stage_and_fails_elsewhere(tmp_path) -> None:
    records = _discard_records(tmp_path)
    assert set(records) == set(CAUSE_STAGE) - {"provider_failure"}
    for cause, rec in records.items():
        assert rec["discard_reason"]["stage"] == CAUSE_STAGE[cause]
        _check(rec)
        wrong = RunStage.INJECT if CAUSE_STAGE[cause] != RunStage.INJECT else RunStage.ALERT
        bad = _set(rec, ("discard_reason", "stage"), wrong)
        with pytest.raises(RecordValidationError, match=f"cause {cause} comes only from stage"):
            _check(bad)


def test_harm_stage_with_injection_failed_is_rejected(good) -> None:
    rec = good
    for k in ("stabilization_timed_out", "postaction_snapshot_hash"):
        rec = _set(rec, (k,), None)
    plausible = _as_discard(rec, RunStage.STABILIZATION)  # a real post-agent discard
    _check(plausible)
    impossible = _as_discard(rec, RunStage.STABILIZATION, "injection_failed")
    with pytest.raises(RecordValidationError, match="comes only from stage inject"):
        _check(impossible)


def test_operator_abort_and_not_implemented_only_under_r2(good) -> None:
    rec = good
    for k in ("stabilization_timed_out", "postaction_snapshot_hash"):
        rec = _set(rec, (k,), None)
    for cause in ("operator_abort", "not_implemented"):
        _check(_as_discard(rec, RunStage.STABILIZATION, cause))  # trajectory + attempt
    pre = _as_discard(_set(good, ("alert",), None), RunStage.ALERT, "operator_abort")
    with pytest.raises(RecordValidationError, match="only under R2"):
        _check(pre)


def test_infrastructure_failure_is_not_a_pre_run_cause(tmp_path) -> None:
    rec = _discard_records(tmp_path)["pre_run_dirty"]
    bad = _set(rec, ("discard_reason", "cause"), "infrastructure_failure")
    with pytest.raises(RecordValidationError, match="is pre_run_dirty"):
        _check(bad)


# --- 3. fault verification: present <-> observations ------------------------------------------


def test_present_must_match_the_held_observations(good) -> None:
    with pytest.raises(RecordValidationError, match="present is True but observations held"):
        _check(_set(good, ("fault_verification", "observations", 0, "held"), False))


def test_absent_with_a_held_observation_is_rejected(tmp_path) -> None:
    rec = _discard_records(tmp_path)["silent_non_injection"]
    obs = rec["fault_verification"]["observations"][0]
    held = {**obs, "held": True, "value": 1.0, "server_error_responses": obs["attempts"]}
    with pytest.raises(RecordValidationError, match="present is False but observations held"):
        _check(_set(rec, ("fault_verification", "observations", 0), held))


def test_each_observation_s_own_numbers_must_agree(good) -> None:
    with pytest.raises(RecordValidationError, match="value != server_error_responses"):
        _check(_set(good, ("fault_verification", "observations", 0, "value"), 0.5))
    obs = good["fault_verification"]["observations"][0]
    low = {**obs, "server_error_responses": 30, "value": 0.1}  # 0.1 is not > 0.2
    with pytest.raises(RecordValidationError, match="held disagrees with value"):
        _check(_set(good, ("fault_verification", "observations", 0), low))


# --- 4. the alert traces to the verified symptom and the first held window ---------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("alert_name", "ServiceDown"),
        ("target_service", "payment"),
        ("symptom", "http_5xx_rate > 0.5 over 30s"),
        ("fired_at", "2026-11-19T18:00:01Z"),
    ],
)
def test_each_alert_field_must_trace_to_the_verification(good, field, value) -> None:
    with pytest.raises(RecordValidationError):
        _check(_set(good, ("alert", field), value))


def test_fired_at_is_the_first_held_observation_not_any_held_one(good) -> None:
    first = copy.deepcopy(good["fault_verification"]["observations"][0])
    second = {**first, "window_id": "w9", "observed_at": "2026-11-19T18:00:30Z"}
    rec = _set(good, ("fault_verification", "observations"), [first, second])
    _check(rec)  # fired_at is the first held observation's time
    late = _set(rec, ("alert", "fired_at"), second["observed_at"])
    with pytest.raises(RecordValidationError, match="first held observation"):
        _check(late)


def test_fired_at_skips_windows_that_did_not_hold(good) -> None:
    held = copy.deepcopy(good["fault_verification"]["observations"][0])
    missed = {
        **held,
        "window_id": "w1",
        "held": False,
        "server_error_responses": 0,
        "value": 0.0,
        "observed_at": "2026-11-19T17:59:30Z",
    }
    rec = _set(good, ("fault_verification", "observations"), [missed, held])
    _check(rec)
    with pytest.raises(RecordValidationError, match="first held observation"):
        _check(_set(rec, ("alert", "fired_at"), missed["observed_at"]))


# --- 5. the sweep -------------------------------------------------------------------------------


SWEEP = {
    "settled with last changes": (
        ("postfault_settledness", "last_changes"),
        ["Service/orders-svc: changed"],
    ),
    "token total disagrees with steps": (("totals", "tokens_in"), 9999),
    "wall seconds disagree with the timestamps": (("totals", "wall_seconds"), 12345.0),
    "confidence shadow scalar is not the proposal's": (
        ("shadow_gates", "confidence", "confidence"),
        0.5,
    ),
    "confidence_deficit is not 1 - confidence": (("feature_vector", "confidence_deficit"), 0.5),
    "harmful without a violation": (("outcome", "harmful"), True),
}


@pytest.mark.parametrize("path,value", SWEEP.values(), ids=SWEEP.keys())
def test_sweep_derived_fields_must_agree_with_their_evidence(good, path, value) -> None:
    rec = _set(good, path, value)
    if path == ("outcome", "harmful"):
        rec = _set(rec, ("outcome", "category"), "harmful_but_effective")
    with pytest.raises(RecordValidationError):
        _check(rec)


def test_an_unsettled_state_may_have_no_last_change(good) -> None:
    """Not pinned: with a timeout shorter than one poll, the producer times out before any
    comparison, so an unsettled state can legitimately report no change."""
    rec = _set(good, ("postfault_settledness", "settled"), False)
    rec = _set(rec, ("postfault_settledness", "last_changes"), [])
    _check(rec)


def test_a_record_s_filename_must_be_its_run_id(tmp_path) -> None:
    _runner(tmp_path).run(UNIT)
    (path,) = (tmp_path / "runs").glob("*.json")
    load_record(path)
    renamed = tmp_path / "00000000-0000-4000-8000-000000000000.json"
    renamed.write_text(path.read_text())
    with pytest.raises(RecordValidationError, match="holds run"):
        load_record(renamed)


def test_records_from_the_runner_still_validate_end_to_end(tmp_path) -> None:
    rec_runner = _runner(tmp_path / "ok")
    rec_runner.run(UNIT)
    (rec,) = _records(tmp_path / "ok")
    _check(rec)
    assert json.loads(json.dumps(rec)) == rec


# --- final review ------------------------------------------------------------------------------


def test_final_reset_verified_follows_the_discard_phase(tmp_path, good) -> None:
    """A pre-agent discard runs no post-run reset; a post-agent one always does first."""
    dirty = _discard_records(tmp_path)["pre_run_dirty"]
    assert dirty["reset_verified"] is None
    with pytest.raises(RecordValidationError, match="pre-agent discard runs no post-run reset"):
        _check(_set(dirty, ("reset_verified",), True))
    rec = good
    for k in ("stabilization_timed_out", "postaction_snapshot_hash"):
        rec = _set(rec, (k,), None)
    post = _as_discard(rec, RunStage.STABILIZATION)
    _check(post)
    with pytest.raises(RecordValidationError, match="post-agent discard is cleaned up first"):
        _check(_set(post, ("reset_verified",), None))


def test_final_the_runner_writes_reset_verified_per_phase(tmp_path) -> None:
    from safeops.experiment.runner import PostAgentDiscard

    r = _runner(tmp_path / "post", harm=Fixed(raises=KeyError("x")))
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    (post,) = _records(tmp_path / "post")
    assert post["reset_verified"] is True


def test_final_no_applied_step_after_an_unapplied_one(good) -> None:
    restarted_anyway = [
        _step("patch_configmap", "failed", "422"),
        _step("restart_deployment", "applied"),
    ]
    with pytest.raises(RecordValidationError, match="applied step follows"):
        _check(_exec(good, "partially_applied", restarted_anyway, None, PATCH_POOL))
    in_order = [_step("patch_configmap", "applied"), _step("restart_deployment", "failed", "x")]
    _check(_exec(good, "partially_applied", in_order, None, PATCH_POOL))


def test_final_record_bounds_match_the_writer(good, tmp_path) -> None:
    from safeops.experiment.records import MAX_DISCARD_ERROR, MAX_LAST_CHANGES

    unsettled = _set(good, ("postfault_settledness", "settled"), False)
    _check(_set(unsettled, ("postfault_settledness", "last_changes"), ["c"] * MAX_LAST_CHANGES))
    with pytest.raises(RecordValidationError):
        _check(
            _set(
                unsettled, ("postfault_settledness", "last_changes"), ["c"] * (MAX_LAST_CHANGES + 1)
            )
        )
    dirty = _discard_records(tmp_path)["pre_run_dirty"]
    _check(_set(dirty, ("discard_reason", "error"), "x" * MAX_DISCARD_ERROR))
    with pytest.raises(RecordValidationError):
        _check(_set(dirty, ("discard_reason", "error"), "x" * (MAX_DISCARD_ERROR + 1)))


def test_final_cli_wording_does_not_deny_an_installed_record(monkeypatch) -> None:
    from typer.testing import CliRunner

    from safeops import cli
    from safeops.experiment import runner
    from safeops.experiment.records import RecordWriteError

    class Installed:
        def run(self, unit):
            raise RecordWriteError(
                "run record x.json is installed, but confirming it durable failed"
            )

    monkeypatch.setattr(runner, "build_runner", lambda: Installed())
    result = CliRunner().invoke(
        cli.app, ["run", "--fault", "bad-deployment-config", "--seed", "0", "--trial", "0"]
    )
    assert result.exit_code == cli.EXIT_RECORD
    assert "not written" not in result.output and "is installed" in result.output
