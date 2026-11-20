"""Milestone 5 review fix list: schema v1 invariants and its lifecycle contract (decision
0018, fix-list amendment). Offline, fabricated inputs."""

from __future__ import annotations

import copy
import json
import os
import stat

import pytest

from safeops.enums import ExecutionStatus, ProposalStatus, RunStage
from safeops.experiment.records import Discard, RecordWriteError, RunRecordWriter, _known_total
from safeops.experiment.runner import PostAgentDiscard, PreRunDiscard
from safeops.experiment.schema import (
    POST_AGENT_STAGES,
    PRE_AGENT_STAGES,
    STAGE_PRODUCES,
    RecordValidationError,
    stage_phase,
)
from tests.test_m5_records import (
    STEP,
    UNIT,
    Agent,
    Fixed,
    Rec,
    Through,
    _as_discard,
    _check,
    _records,
    _runner,
    _set,
    _strip_agent,
)


@pytest.fixture
def good(tmp_path) -> dict:
    """A fabricated perfect run's record (as in test_m5_records)."""
    _runner(tmp_path / "good").run(UNIT)
    (rec,) = _records(tmp_path / "good")
    return rec


# --- 1. run_id is allocated at run start and carried through -----------------------------------


class _Seeds:
    """A shadow-gate double that captures the run_id it is given."""

    def __init__(self):
        self.run_ids: list[str] = []

    def score(self, features, run_id):
        self.run_ids.append(run_id)
        return Rec(
            {
                "safeops": {"risk_score": 0.1},
                "confidence": {"confidence": 0.82},
                "random": {"draw": 0.5},
            }
        ).to_record()


def test_run_id_exists_before_the_shadow_gates_and_is_the_record_and_filename(tmp_path) -> None:
    seeds = _Seeds()
    r = _runner(tmp_path, shadow_gates=seeds)
    r.run(UNIT)
    (path,) = (tmp_path / "runs").glob("*.json")
    rec = json.loads(path.read_text())
    assert seeds.run_ids == [rec["run_id"]] == [path.stem] == [r.current_run_id]


def test_run_id_reaches_the_writer_unchanged(tmp_path) -> None:
    r = _runner(tmp_path)
    seen = []
    real = r.records.finalize
    r.records.finalize = lambda **f: (seen.append(f["run_id"]), real(**f))[1]
    r._new_run_id = lambda: "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
    r.run(UNIT)
    assert seen == ["6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"]
    assert (tmp_path / "runs" / "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b.json").is_file()


def test_the_writer_never_generates_a_run_id(tmp_path) -> None:
    from tests.conftest import FixedProvenance

    w = RunRecordWriter(runs_dir=tmp_path, provenance=FixedProvenance())
    with pytest.raises(TypeError, match="run_id allocated at the start"):
        w.finalize(unit=UNIT, provenance=FixedProvenance().collect())


def test_recovery_diagnostics_name_the_run(tmp_path) -> None:
    r = _runner(tmp_path)
    r.environment.clean = False
    reasons = []
    r.environment.quarantine_and_rebuild = lambda reason="": reasons.append(reason)
    with pytest.raises(PreRunDiscard):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["run_id"] in reasons[0]


# --- 2. run stages are a closed enum, partitioned exactly ----------------------------------------


def test_pre_and_post_agent_stages_partition_every_run_stage_exactly_once() -> None:
    """Checked against the enum itself, so a member left out of both tuples fails."""
    pre, post = set(PRE_AGENT_STAGES), set(POST_AGENT_STAGES)
    assert not pre & post
    assert pre | post == set(RunStage)
    assert len(PRE_AGENT_STAGES) + len(POST_AGENT_STAGES) == len(RunStage)
    assert tuple(STAGE_PRODUCES) == tuple(RunStage) == PRE_AGENT_STAGES + POST_AGENT_STAGES


def test_an_unknown_stage_fails_at_once_never_guessed_pre_agent() -> None:
    with pytest.raises(ValueError):
        Discard("harm_chek", "infrastructure_failure", "x")
    with pytest.raises(ValueError, match="not classified"):
        stage_phase("lunch")  # type: ignore[arg-type]
    assert Discard("harm", "infrastructure_failure", "x").stage is RunStage.HARM


def test_the_runner_names_stages_only_through_the_enum() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "safeops/experiment/runner.py").read_text()
    assert 'stage = "' not in src and "stage = RunStage." in src


# --- 3 and 5. every ProposalStatus and ExecutionStatus, fabricated -------------------------------


def _completed_without_gate(rec: dict, status: str, proposal) -> dict:
    for k in ("feature_vector", "gate", "shadow_gates", "execution"):
        rec = _set(rec, (k,), None)
    rec = _set(rec, ("action_attempted",), False)
    rec = _set(rec, ("in_cohort_g",), False)
    rec = _set(rec, ("outcome", "category"), None)
    rec = _set(rec, ("proposal_status",), status)
    return _set(rec, ("proposal",), proposal)


def _no_action_proposal(rec: dict) -> dict:
    p = copy.deepcopy(rec["proposal"])
    p["recommended_action"] = None
    return p


def test_valid_action_applied_is_cohort_g(good) -> None:
    _check(good)
    assert good["proposal_status"] == ProposalStatus.VALID_ACTION and good["in_cohort_g"]


def test_no_action_is_a_proposal_with_a_null_action(good) -> None:
    """§11.4: recommended_action = None means no mutation warranted."""
    _check(_completed_without_gate(good, "no_action", _no_action_proposal(good)))
    with pytest.raises(RecordValidationError, match="no_action must be a proposal"):
        _check(_completed_without_gate(good, "no_action", None))
    with pytest.raises(RecordValidationError, match="no_action must be a proposal"):
        _check(_completed_without_gate(good, "no_action", good["proposal"]))


@pytest.mark.parametrize("status", ["invalid_output", "step_limit", "model_refusal"])
def test_statuses_without_a_proposal_complete_without_one(good, status) -> None:
    _check(_completed_without_gate(good, status, None))
    with pytest.raises(RecordValidationError, match="a proposal is stored"):
        _check(_completed_without_gate(good, status, good["proposal"]))


def _provider_failure(good: dict, **reason) -> dict:
    rec = _completed_without_gate(good, "provider_failure", None)
    for k in ("stabilization_timed_out", "postaction_snapshot_hash", "reset_verified"):
        rec = _set(rec, (k,), None)
    rec = _as_discard(rec, RunStage.ROUTE_PROPOSAL, reason.get("cause", "provider_failure"))
    return rec


def test_provider_failure_is_a_discard_outside_g_with_no_proposal(good) -> None:
    _check(_provider_failure(good))
    not_discarded = _completed_without_gate(good, "provider_failure", None)
    with pytest.raises(RecordValidationError, match="provider_failure must be a discard"):
        _check(not_discarded)
    with pytest.raises(RecordValidationError, match="provider_failure must be a discard"):
        _check(_provider_failure(good, cause="infrastructure_failure"))
    with pytest.raises(RecordValidationError):
        _check(_set(_provider_failure(good), ("proposal",), good["proposal"]))


def test_every_proposal_status_is_covered_above() -> None:
    covered = {
        "valid_action",
        "no_action",
        "invalid_output",
        "step_limit",
        "model_refusal",
        "provider_failure",
    }
    assert covered == {s.value for s in ProposalStatus}


# Steps each status really produces (the cross-field review): a refusal never reaches the
# API, and partial application exists only for the two-step composite.
_STEPS = {
    "refused_by_envelope": [],
    "rejected_invalid_action": [
        {"operation": "rollback_deployment", "status": "failed", "error": "422"}
    ],
    "infrastructure_failure": [],
}
PATCH_POOL = {
    "type": "patch_configmap",
    "target": {"kind": "ConfigMap", "ns": "testbed", "name": "db-pool-config"},
    "params": {"max_pool_size": 20},
}


def _with_execution(good: dict, status: str) -> dict:
    rec = _set(good, ("execution", "status"), status)
    if status == "partially_applied":
        rec = _set(rec, ("proposal", "recommended_action"), PATCH_POOL)
        rec = _set(
            rec,
            ("execution", "steps"),
            [
                {"operation": "patch_configmap", "status": "applied", "error": None},
                {"operation": "restart_deployment", "status": "failed", "error": "timeout"},
            ],
        )
    elif status in _STEPS:
        rec = _set(rec, ("execution", "steps"), _STEPS[status])
    in_g = status == "applied"
    rec = _set(rec, ("in_cohort_g",), in_g)
    return _set(rec, ("outcome", "category"), good["outcome"]["category"] if in_g else None)


@pytest.mark.parametrize(
    "status", ["partially_applied", "refused_by_envelope", "rejected_invalid_action"]
)
def test_real_execution_outcomes_are_kept_not_discarded(good, status) -> None:
    rec = _with_execution(good, status)
    _check(rec)
    assert rec["discarded"] is False and rec["in_cohort_g"] is False


def test_executor_infrastructure_failure_must_be_a_discard(good) -> None:
    rec = _with_execution(good, "infrastructure_failure")
    with pytest.raises(RecordValidationError, match="executor infrastructure_failure"):
        _check(rec)
    for k in ("stabilization_timed_out", "postaction_snapshot_hash"):
        rec = _set(rec, (k,), None)
    _check(_as_discard(rec, RunStage.EXECUTED_ACTION, "infrastructure_failure"))


def test_every_execution_status_is_covered_above() -> None:
    covered = {
        "applied",
        "partially_applied",
        "refused_by_envelope",
        "rejected_invalid_action",
        "infrastructure_failure",
    }
    assert covered == {s.value for s in ExecutionStatus}


class _NoGate(Through):
    def _gate_eligible_action(self, proposal):
        return None


@pytest.mark.parametrize("status", ["no_action", "invalid_output", "step_limit", "model_refusal"])
def test_each_parser_status_then_a_later_failure_writes_a_valid_record(tmp_path, status) -> None:
    from tests.conftest import FakeEnvironment, FakeInjector, FakeWorkload

    base = _runner(tmp_path)
    proposal = None
    if status == "no_action":
        proposal = {
            "diagnosis": "transient; recovers on its own",
            "recommended_action": None,
            "confidence": 0.6,
            "evidence": [1],
            "prerequisites_claimed": [],
        }
    r = _NoGate(
        environment=FakeEnvironment(),
        workload=FakeWorkload(),
        injector=FakeInjector(),
        alerts=base.alerts,
        agent=Agent(),
        parser=Fixed(Rec({"proposal_status": status, "proposal": proposal})),
        extractor=base.extractor,
        shadow_gates=base.shadow_gates,
        gate=base.gate,
        executor=base.executor,
        effectiveness=base.effectiveness,
        harm=Fixed(raises=KeyError("harm crashed")),
        records=base.records,
    )
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["proposal_status"] == status and rec["discard_reason"]["stage"] == "harm"
    assert (rec["proposal"] is None) == (status != "no_action")


# --- 4. Phase A's online gate is always-act ------------------------------------------------------


@pytest.mark.parametrize(
    "path,value",
    [
        (("gate", "strategy"), "safeops"),
        (("gate", "strategy"), "random-p"),
        (("gate", "decision"), "escalate"),
        (("gate", "score"), 0.37),
    ],
    ids=["strategy safeops", "strategy random-p", "online escalate", "online score"],
)
def test_the_online_gate_is_always_act(good, path, value) -> None:
    with pytest.raises(RecordValidationError):
        _check(_set(good, path, value))


def test_always_act_still_holds_on_a_run_discarded_after_execution(good) -> None:
    rec = good
    for k in ("stabilization_timed_out", "postaction_snapshot_hash"):
        rec = _set(rec, (k,), None)
    discarded = _as_discard(rec, RunStage.STABILIZATION)
    _check(discarded)
    with pytest.raises(RecordValidationError):
        _check(_set(discarded, ("gate", "strategy"), "safeops"))


# --- 6. unknown token counts stay unknown --------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "total"),
    [([1200, 80], 1280), ([5, None], None), ([None, None], None), ([0, 0], 0), ([], 0)],
    ids=["all known", "mixed", "all null", "known zero", "no steps"],
)
def test_token_totals_preserve_unknown(counts, total) -> None:
    assert _known_total([{"tokens_in": c} for c in counts], "tokens_in") == total
    assert _known_total(None, "tokens_in") is None


def test_a_mixed_trajectory_records_null_totals(tmp_path) -> None:
    second = copy.deepcopy(STEP.record)
    second.update(step=2, tokens_in=None, tokens_out=40)

    class Mixed(Agent):
        def investigate(self, context):
            return "raw", [STEP, Rec(second)]

    _runner(tmp_path, agent=Mixed()).run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["totals"]["tokens_in"] is None  # 1200 + unknown is unknown, never 1200
    assert rec["totals"]["tokens_out"] == 120


# --- 7. a partial trajectory survives an agent crash (schema representability) -------------------


def test_a_partial_trajectory_with_no_proposal_is_a_valid_discard(good) -> None:
    rec = _strip_agent(good)
    rec = _set(rec, ("trajectory",), good["trajectory"])  # steps done before the crash
    rec = _set(rec, ("totals", "tokens_in"), good["totals"]["tokens_in"])
    rec = _set(rec, ("totals", "tokens_out"), good["totals"]["tokens_out"])
    rec = _as_discard(rec, RunStage.AGENT)
    _check(rec)
    with pytest.raises(RecordValidationError, match="proposal_status from stage parse"):
        _check(_set(rec, ("proposal_status",), "valid_action"))


def test_an_empty_sink_is_not_r2(tmp_path) -> None:
    """R2 starts at the first recorded step: an agent stub with an empty sink writes nothing."""
    from safeops.stubs import NotImplementedStage

    class EmptySink(Agent):
        def investigate(self, context):
            raise NotImplementedStage("Agent.investigate", 9)

    r = _runner(tmp_path, agent=EmptySink())
    with pytest.raises(NotImplementedStage):
        r.run(UNIT)
    assert _records(tmp_path) == []


# --- 8. steady-state arithmetic on the model, denominator always `size` -------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"succeeded": 301, "scheduled": 300, "success_rate": None},
        {"scheduled": 301},
        {"min_successes": 301},
        {"success_rate": 0.5},
        {"state": "timeout", "scheduled": 150, "succeeded": 100, "success_rate": 100 / 150},
        {"success_rate": 1.0, "succeeded": None},
    ],
    ids=[
        "succeeded > scheduled",
        "scheduled > size",
        "min_successes > size",
        "rate != succeeded/size",
        "timeout rate over scheduled",
        "rate without succeeded",
    ],
)
def test_malformed_steady_state_arithmetic_is_rejected(good, changes) -> None:
    ss = {**good["steady_state"], **changes}
    with pytest.raises(RecordValidationError, match="steady_state"):
        _check(_set(good, ("steady_state",), ss))


def test_an_incomplete_window_still_uses_size_as_the_denominator(tmp_path) -> None:
    """A timed-out window scheduled 150 of 300; its rate is over 300, and it is a valid
    no_steady_state discard."""
    from safeops.environment.workload import SteadyState, SteadyStateMeasurementFailed

    class Timeout:
        def start_workload(self):
            pass

        def verify_steady_state(self):
            return SteadyState("w1", "timeout", 300, 150, 100, 297, 100 / 300, None, "late")

        def stop_workload(self):
            pass

    r = _runner(tmp_path, workload=Timeout())
    with pytest.raises(SteadyStateMeasurementFailed):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["discard_reason"]["cause"] == "no_steady_state"
    bad = _set(rec, ("steady_state", "success_rate"), 100 / 150)
    with pytest.raises(RecordValidationError, match="succeeded/size"):
        _check(bad)  # the same malformed window inside a discarded record


# --- 9. write errors say whether the record was installed ----------------------------------------


def test_a_failed_directory_sync_reports_an_installed_record(tmp_path, monkeypatch) -> None:
    from safeops.experiment import records as records_mod

    real = records_mod._durable_sync

    def failing_on_dirs(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("EIO on directory fsync")
        real(fd)

    monkeypatch.setattr(records_mod, "_durable_sync", failing_on_dirs)
    with pytest.raises(RecordWriteError, match="is installed, but confirming it durable failed"):
        RunRecordWriter._write_once(tmp_path, "r1", "{}\n")
    final = tmp_path / "r1.json"
    assert final.read_text() == "{}\n" and stat.S_IMODE(os.stat(final).st_mode) == 0o444


def test_a_failure_before_the_link_reports_not_installed(tmp_path, monkeypatch) -> None:
    from safeops.experiment import records as records_mod

    def failing(fd):
        raise OSError("EIO on file fsync")

    monkeypatch.setattr(records_mod, "_durable_sync", failing)
    with pytest.raises(RecordWriteError, match="was not installed"):
        RunRecordWriter._write_once(tmp_path, "r2", "{}\n")
    assert not (tmp_path / "r2.json").exists()


# --- 10. an earlier interrupt survives later cleanup blocks ------------------------------------


@pytest.mark.parametrize(
    ("stop_raises", "reset_raises", "kept"),
    [
        (KeyboardInterrupt(), RuntimeError("reset failed"), KeyboardInterrupt),
        (RuntimeError("stop failed"), SystemExit(2), SystemExit),
        (KeyboardInterrupt(), SystemExit(2), KeyboardInterrupt),  # the first one wins
    ],
)
def test_cleanup_never_loses_an_interrupt(tmp_path, stop_raises, reset_raises, kept) -> None:
    r = _runner(tmp_path)

    def stop():
        raise stop_raises

    def reset():
        raise reset_raises

    r.workload.stop_workload = stop
    r.environment.reset_environment = reset
    post_clean, interrupt = r._cleanup()
    assert isinstance(interrupt, kept) and not post_clean.clean
