"""Reset's mutation boundary and the pre-run lifecycle (decision 0022). Offline.

Before reset's first mutating call, a failure propagates as itself: nothing was changed.
From that call on, any ordinary failure is a ResetMutationError. In the runner, that, a
failure of the verification after a reset, and an ordinary dirty result all take the one
canonical path: _require_pre_run_clean -> PreRunDiscard -> _finalize_then_recover, which
writes the record first and quarantines second.
"""

from __future__ import annotations

import copy
import inspect
import subprocess

import pytest

from safeops.cluster import CommandError, CredentialError, GuardError
from safeops.environment.manager import (
    MAX_RESET_ERROR,
    ConfigurationError,
    EnvironmentManager,
    QuarantineFailed,
    ResetMutationError,
    WorkerQuarantined,
)
from safeops.experiment import runner as runner_mod
from safeops.experiment.runner import ExperimentRunner, PreRunDiscard
from tests.test_lifecycle import FakeKubectl, _cfg, _desired_objects, _env
from tests.test_m5_records import UNIT, _records, _runner

# --- the manager: where the boundary is -------------------------------------------------------


class FailingKubectl(FakeKubectl):
    """Live state equals desired; the Nth mutating call (1-based) raises `exc`. With
    fail_read_after set, the first read after that many mutations raises instead."""

    def __init__(self, live, *, fail_at=None, exc=None, fail_read_after=None, chaos=None):
        super().__init__(live, chaos=chaos)
        self.fail_at, self.exc, self.fail_read_after = fail_at, exc, fail_read_after
        self.mutations = 0

    def _mutation(self, op):
        self.mutations += 1
        if self.mutations == self.fail_at:
            raise self.exc
        op()

    def delete(self, *a):
        self._mutation(lambda: super(FailingKubectl, self).delete(*a))

    def create(self, objs):
        self._mutation(lambda: super(FailingKubectl, self).create(objs))

    def replace(self, objs):
        self._mutation(lambda: super(FailingKubectl, self).replace(objs))

    def get_json(self, kinds, *rest):
        if self.fail_read_after is not None and self.mutations >= self.fail_read_after:
            raise self.exc
        return super().get_json(kinds, *rest)


def _manager(tmp_path, **kw) -> tuple[EnvironmentManager, FailingKubectl]:
    k = FailingKubectl(copy.deepcopy(_desired_objects(_cfg(tmp_path))), **kw)
    env = _env(tmp_path, kubectl=k)
    env.wait_stabilization_window = lambda: True
    return env, k


ORDINARY_FAILURES = {
    "credential": CredentialError("experiment-controller credential rejected (Unauthorized)"),
    "guard": GuardError("kubectl 'create' is outside the runtime scope"),
    "command": CommandError("kubectl create failed: connection refused"),
    "timeout": subprocess.TimeoutExpired(cmd="kubectl create", timeout=120),
    "runtime": RuntimeError("unexpected kubectl output"),
}


def test_a_full_reset_crosses_the_boundary_and_succeeds(tmp_path) -> None:
    env, k = _manager(tmp_path)
    env.reset_environment()
    assert k.mutations > 3 and k.ops  # the app tier is always recreated


@pytest.mark.parametrize("name", sorted(ORDINARY_FAILURES))
def test_any_ordinary_failure_after_the_first_mutation_is_a_reset_mutation_error(
    tmp_path, name
) -> None:
    """Earlier mutations succeeded; a later one fails. A timeout is no exception: it never
    proves the API server didn't apply the call."""
    exc = ORDINARY_FAILURES[name]
    env, k = _manager(tmp_path, fail_at=4, exc=exc)
    with pytest.raises(ResetMutationError) as caught:
        env.reset_environment()
    assert len(k.ops) == 3  # three mutations had applied: the environment is half reset
    assert caught.value.__cause__ is exc
    assert type(exc).__name__ in str(caught.value)
    assert caught.value.step.startswith(("delete ", "create ", "replace "))


def test_a_read_after_the_boundary_is_named_as_the_failing_step(tmp_path) -> None:
    exc = CredentialError("token expired")
    chaos = [{"metadata": {"name": "delay-orders"}}]
    env, k = _manager(tmp_path, exc=exc, fail_read_after=1, chaos=chaos)
    with pytest.raises(ResetMutationError, match="list unexpected objects") as caught:
        env.reset_environment()
    assert k.ops == [("delete", "NetworkChaos", "delay-orders")]  # the chaos delete applied
    assert caught.value.step == "list unexpected objects" and caught.value.__cause__ is exc


def test_the_first_mutation_failing_is_already_past_the_boundary(tmp_path) -> None:
    """The flag is set before the call: a call that raises may still have applied."""
    env, k = _manager(tmp_path, fail_at=1, exc=CommandError("kubectl delete failed: timed out"))
    with pytest.raises(ResetMutationError):
        env.reset_environment()


@pytest.mark.parametrize("name", ["credential", "guard", "timeout"])
def test_a_read_failure_before_any_mutation_propagates_as_itself(tmp_path, name) -> None:
    exc = ORDINARY_FAILURES[name]
    env, k = _manager(tmp_path, exc=exc, fail_read_after=0)
    env._chaos_objects = lambda: []  # the first projected-object read is the first read
    with pytest.raises(type(exc)) as caught:
        env.reset_environment()
    assert not isinstance(caught.value, ResetMutationError)
    assert k.ops == [] and k.mutations == 0


def test_a_configuration_failure_propagates_before_anything_is_touched(tmp_path) -> None:
    env, k = _manager(tmp_path)

    def stale():
        raise ConfigurationError("config/desired_state.yaml is stale")

    env._configuration = stale
    with pytest.raises(ConfigurationError):
        env.reset_environment()
    assert k.ops == [] and k.mutations == 0


def test_a_quarantined_worker_refuses_before_anything_is_touched(tmp_path) -> None:
    env, k = _manager(tmp_path)
    env._marker.parent.mkdir(parents=True, exist_ok=True)
    env._marker.write_text('{"reason": "x"}')
    with pytest.raises(WorkerQuarantined):
        env.reset_environment()
    assert k.ops == [] and k.mutations == 0


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(1)])
def test_interrupts_are_never_wrapped(tmp_path, interrupt) -> None:
    env, k = _manager(tmp_path, fail_at=4, exc=interrupt)
    with pytest.raises(type(interrupt)) as caught:
        env.reset_environment()
    assert caught.value is interrupt


def test_the_underlying_error_is_bounded(tmp_path) -> None:
    exc = CommandError("x" * 50_000)
    env, _ = _manager(tmp_path, fail_at=2, exc=exc)
    with pytest.raises(ResetMutationError) as caught:
        env.reset_environment()
    assert len(str(caught.value)) < MAX_RESET_ERROR + 200
    assert "[50000 chars, truncated]" in str(caught.value)
    assert caught.value.__cause__ is exc  # the whole original stays reachable, unrecorded


# --- the runner: three ways to fail the pre-run check, one path ---------------------------------


class Spy:
    """Records the lifecycle calls, in order, on a real runner and its fake environment."""

    def __init__(self, r: ExperimentRunner, *, quarantine_raises=None, finalize_raises=None):
        self.events: list[str] = []
        real_require, real_ftr = r._require_pre_run_clean, r._finalize_then_recover
        real_finalize = r.records.finalize

        def require(clean):
            self.events.append("require_pre_run_clean")
            return real_require(clean)

        def ftr(*a, **kw):
            self.events.append("finalize_then_recover")
            return real_ftr(*a, **kw)

        def finalize(**fields):
            self.events.append("finalize")
            if finalize_raises is not None:
                raise finalize_raises
            return real_finalize(**fields)

        def quarantine(reason=""):
            self.events.append("quarantine")
            self.reason = reason
            if quarantine_raises is not None:
                raise quarantine_raises

        r._require_pre_run_clean, r._finalize_then_recover = require, ftr
        r.records.finalize = finalize
        r.environment.quarantine_and_rebuild = quarantine


def _dirty(r):
    r.environment.clean = False


def _reset_mutation_error(r):
    def reset():
        raise ResetMutationError(
            "create Service objects", CredentialError("credential rejected (Unauthorized)")
        )

    r.environment.reset_environment = reset


def _verify_fails(r):
    def verify():
        raise CredentialError("credential rejected (Unauthorized)")

    r.environment.verify_clean_state = verify


PATHS = {
    "dirty result": (_dirty, "Deployment/orders: drift"),
    "reset mutation error": (_reset_mutation_error, "at create Service objects: CredentialError"),
    "verify fails after reset": (
        _verify_fails,
        "verification failed after reset: CredentialError",
    ),
}
CANONICAL = ["require_pre_run_clean", "finalize_then_recover", "finalize", "quarantine"]


@pytest.mark.parametrize("path", sorted(PATHS))
def test_every_pre_run_failure_takes_the_one_canonical_path(tmp_path, path) -> None:
    setup, detail = PATHS[path]
    r = _runner(tmp_path)
    setup(r)
    spy = Spy(r)
    with pytest.raises(PreRunDiscard, match="environment not clean before the run"):
        r.run(UNIT)
    assert spy.events == CANONICAL  # record first, then quarantine, through one handler
    (rec,) = _records(tmp_path)  # exactly one record
    assert rec["discarded"] is True
    assert rec["discard_reason"]["cause"] == "pre_run_dirty"
    assert rec["discard_reason"]["stage"] == "pre_run_clean_state"
    assert rec["discard_reason"]["phase"] == "pre_agent"
    assert rec["clean_state_verified"] is False
    assert detail in rec["discard_reason"]["error"]  # the failing operation is recoverable
    assert detail in spy.reason  # and named in the quarantine reason


@pytest.mark.parametrize("path", sorted(PATHS))
def test_a_failed_quarantine_halts_the_worker_after_the_record(tmp_path, path) -> None:
    setup, _ = PATHS[path]
    r = _runner(tmp_path)
    setup(r)
    spy = Spy(r, quarantine_raises=QuarantineFailed("cluster-down failed"))
    with pytest.raises(QuarantineFailed):
        r.run(UNIT)
    assert spy.events == CANONICAL
    assert len(_records(tmp_path)) == 1  # one record, not one per nested handler
    calls = list(r.environment.calls)
    with pytest.raises(QuarantineFailed):
        r.run(UNIT)  # the next unit is refused before anything runs
    assert r.environment.calls == calls and len(_records(tmp_path)) == 1


@pytest.mark.parametrize("path", sorted(PATHS))
def test_a_record_failure_still_recovers_then_surfaces(tmp_path, path) -> None:
    """The existing invariant (M5 second review, finding 3), on every path alike."""
    setup, _ = PATHS[path]
    r = _runner(tmp_path)
    setup(r)
    spy = Spy(r, finalize_raises=OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        r.run(UNIT)
    assert spy.events == CANONICAL


def test_a_partial_reset_end_to_end_is_recorded_then_quarantined(tmp_path) -> None:
    """The defect itself, through the real EnvironmentManager: three mutations apply, the
    fourth hits an expired credential. Before decision 0022 this propagated as a bare
    CredentialError: no record, no quarantine, a half-reset worker."""
    env, k = _manager(tmp_path, fail_at=4, exc=CredentialError("credential rejected"))
    r = _runner(tmp_path)
    r.environment = env
    spy = Spy(r)
    with pytest.raises(PreRunDiscard):
        r.run(UNIT)
    assert len(k.ops) == 3
    assert spy.events == CANONICAL
    (rec,) = _records(tmp_path)
    assert rec["discard_reason"]["cause"] == "pre_run_dirty"
    assert rec["clean_state_verified"] is False
    assert "reset failed after it began mutating" in rec["discard_reason"]["error"]


def test_a_preflight_failure_writes_nothing_and_never_quarantines(tmp_path) -> None:
    env, k = _manager(tmp_path, exc=CredentialError("expired"), fail_read_after=0)
    env._chaos_objects = lambda: []
    r = _runner(tmp_path)
    r.environment = env
    spy = Spy(r)
    with pytest.raises(CredentialError):
        r.run(UNIT)
    assert spy.events == [] and _records(tmp_path) == [] and k.mutations == 0


@pytest.mark.parametrize("where", ["reset", "verify"])
def test_an_interrupt_in_the_pre_run_check_is_unchanged(tmp_path, where) -> None:
    """Ctrl-C before the run is an operator abort: no record, no quarantine (R1)."""
    r = _runner(tmp_path)

    def interrupted():
        raise KeyboardInterrupt

    setattr(
        r.environment,
        f"{where}_environment" if where == "reset" else "verify_clean_state",
        interrupted,
    )
    spy = Spy(r)
    with pytest.raises(KeyboardInterrupt):
        r.run(UNIT)
    assert spy.events == [] and _records(tmp_path) == []


def test_no_second_record_or_recovery_handler_exists() -> None:
    """Structural: the new cases add a conversion, not a lifecycle."""
    src = inspect.getsource(runner_mod)
    assert src.count("self.records.finalize(") == 1  # only _finalize writes
    assert src.count("self._quarantine(") == 2  # the pre-run handler and _recover_if_dirty
    assert src.count("self._finalize_then_recover(") == 3  # pre-run, post-run, post-agent
    conversion = inspect.getsource(ExperimentRunner._pre_run_clean_state)
    for forbidden in ("finalize", "quarantine", "_recover", "PreRunDiscard(", "Discard("):
        assert forbidden not in conversion.split('"""')[2], forbidden


def test_the_verification_failure_detail_is_bounded(tmp_path) -> None:
    r = _runner(tmp_path)

    def verify():
        raise CredentialError("y" * 50_000)

    r.environment.verify_clean_state = verify
    r.environment.quarantine_and_rebuild = lambda reason="": None
    with pytest.raises(PreRunDiscard):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert len(rec["discard_reason"]["error"]) <= 2000
