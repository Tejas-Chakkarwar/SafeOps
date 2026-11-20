"""RUN_SEQUENCE (the dry-run listing) must match what ExperimentRunner.run actually calls,
on every branch, with every call matching the callee's declared signature.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from safeops.experiment.runner import ExperimentRunner, RunUnit
from safeops.experiment.sequence import EXECUTE_ONLY, RUN_SEQUENCE, VALID_ONLY, first_stub
from safeops.stubs import NotImplementedStage

UNIT = RunUnit(fault_id="bad-deployment-config", fault_seed=0, trial_index=0)

# Constructor argument -> component class, taken from the real type hints.
COMPONENTS = {
    name: cls
    for name, cls in typing.get_type_hints(ExperimentRunner.__init__).items()
    if name != "return"
}


class _Recorder:
    """Records (Owner, method) calls and checks each call against the real signature."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def double(self, owner: type) -> object:
        recorder = self

        class Double:
            def __getattr__(self, method: str):
                real = getattr(owner, method)  # AttributeError if the method doesn't exist

                def call(*args, **kwargs):
                    inspect.signature(real).bind(None, *args, **kwargs)  # TypeError on mismatch
                    recorder.calls.append((owner.__name__, method))
                    return (object(), object()) if method == "investigate" else object()

                return call

        return Double()


def _traced_runner(recorder: _Recorder, *, eligible: bool, execute: bool) -> ExperimentRunner:
    """Runner whose hooks record their calls and steer which branch the run takes."""

    def hook(name: str, result):
        real = getattr(ExperimentRunner, name)

        def method(self, *args, **kwargs):
            inspect.signature(real).bind(self, *args, **kwargs)
            recorder.calls.append(("ExperimentRunner", name))
            return result

        return method

    hooks = {
        "_require_pre_run_clean": None,
        "_require_steady_state": None,
        "_require_fault_present": None,
        "_runtime_context": object(),
        "_gate_eligible_action": object() if eligible else None,
        "_should_execute": execute,
        "_executed_action": object(),
        "_post_run_clean": True,
        "_recover_if_dirty": None,
    }
    traced = type(
        "TracedRunner",
        (ExperimentRunner,),
        {name: hook(name, result) for name, result in hooks.items()},
    )
    return traced(**{arg: recorder.double(cls) for arg, cls in COMPONENTS.items()})


PATHS = {
    "valid_action-and-execute": (True, True, set()),
    "valid_action-and-escalate": (True, False, {EXECUTE_ONLY}),
    "not-gate-eligible": (False, False, {VALID_ONLY, EXECUTE_ONLY}),
}


@pytest.mark.parametrize(("eligible", "execute", "skipped"), PATHS.values(), ids=PATHS.keys())
def test_sequence_matches_runner_on_every_path(
    eligible: bool, execute: bool, skipped: set[str]
) -> None:
    recorder = _Recorder()
    _traced_runner(recorder, eligible=eligible, execute=execute).run(UNIT)
    expected = [(s.owner.__name__, s.method) for s in RUN_SEQUENCE if s.when not in skipped]
    assert recorder.calls == expected


def test_branch_labels_are_known() -> None:
    assert {s.when for s in RUN_SEQUENCE} == {"", VALID_ONLY, EXECUTE_ONLY}


def test_every_stage_resolves_to_a_real_method() -> None:
    for stage in RUN_SEQUENCE:
        assert callable(getattr(stage.owner, stage.method)), stage.qualname
        assert stage.milestone is None or 2 <= stage.milestone <= 17, stage.qualname


def test_runner_stops_at_first_stub_in_sequence_order(offline_runner_factory) -> None:
    """Implemented cluster stages are faked (offline); the first remaining stub raises."""
    expected = first_stub()
    assert expected is not None
    with pytest.raises(NotImplementedStage) as exc:
        offline_runner_factory().run(UNIT)
    assert exc.value.stage == expected.qualname
    assert exc.value.milestone == expected.milestone


def test_milestone3_first_stub_is_fault_injection() -> None:
    """After Milestone 3 every lifecycle stage is real; the next stub is Milestone 4's injector.
    (At Milestone 1 it was reset_environment; decision 0004.)"""
    stage = first_stub()
    assert stage is not None
    assert (stage.qualname, stage.milestone) == ("FaultInjector.inject", 4)
