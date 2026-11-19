"""RUN_SEQUENCE (the dry-run listing) must match what ExperimentRunner.run actually calls."""

from __future__ import annotations

import pytest

from safeops.experiment.runner import ExperimentRunner, RunUnit, build_runner
from safeops.experiment.sequence import RUN_SEQUENCE, first_stub
from safeops.stubs import NotImplementedStage

UNIT = RunUnit(fault_id="bad-deployment-config", fault_seed=0, trial_index=0)


class _Recorder:
    """Component doubles that record (Owner, method) and return opaque sentinels."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def double(self, owner: str) -> object:
        recorder = self

        class Double:
            def __getattr__(self, method: str):
                def call(*args, **kwargs):
                    recorder.calls.append((owner, method))
                    return (object(), object()) if method == "investigate" else object()

                return call

        return Double()


def _traced_runner(recorder: _Recorder) -> ExperimentRunner:
    """Runner whose hooks record and take the full path (valid_action, execute)."""

    def hook(name: str, result):
        def method(self, *args, **kwargs):
            recorder.calls.append(("ExperimentRunner", name))
            return result

        return method

    hooks = {
        "_require_pre_run_clean": None,
        "_require_steady_state": None,
        "_require_fault_present": None,
        "_runtime_context": object(),
        "_gate_eligible_action": object(),
        "_should_execute": True,
        "_executed_action": object(),
        "_post_run_clean": True,
    }
    traced = type(
        "TracedRunner",
        (ExperimentRunner,),
        {name: hook(name, result) for name, result in hooks.items()},
    )
    components = {
        "environment": "EnvironmentManager",
        "workload": "WorkloadGenerator",
        "injector": "FaultInjector",
        "alerts": "AlertGenerator",
        "agent": "Agent",
        "parser": "ProposalParser",
        "extractor": "RiskFeatureExtractor",
        "shadow_gates": "ShadowGateScorer",
        "gate": "AlwaysActGate",
        "executor": "ActionExecutor",
        "effectiveness": "EffectivenessVerifier",
        "harm": "HarmChecker",
        "records": "RunRecordWriter",
    }
    return traced(**{arg: recorder.double(owner) for arg, owner in components.items()})


def test_sequence_matches_runner_call_order() -> None:
    recorder = _Recorder()
    _traced_runner(recorder).run(UNIT)
    assert recorder.calls == [(s.owner.__name__, s.method) for s in RUN_SEQUENCE]


def test_every_stage_resolves_to_a_real_method() -> None:
    for stage in RUN_SEQUENCE:
        assert callable(getattr(stage.owner, stage.method)), stage.qualname
        assert stage.milestone is None or 2 <= stage.milestone <= 17, stage.qualname


def test_real_runner_stops_at_first_stub_in_sequence_order() -> None:
    expected = first_stub()
    assert expected is not None
    with pytest.raises(NotImplementedStage) as exc:
        build_runner().run(UNIT)
    assert exc.value.stage == expected.qualname
    assert exc.value.milestone == expected.milestone


def test_milestone1_first_stub_is_reset_environment() -> None:
    """Milestone 1 acceptance (decision 0004). Revisit when Milestone 3 implements reset."""
    stage = first_stub()
    assert stage is not None
    assert (stage.qualname, stage.milestone) == ("EnvironmentManager.reset_environment", 3)
