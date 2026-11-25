"""Milestone 8A-1: routing on proposal_status, through the real runner (decision 0025
D§3.1, D§4; decision 0018).

The real ProposalParser and the real _gate_eligible_action, fed fabricated AgentOutput
envelopes (Rule 17). The harness around them is the offline fakes. A valid_action run is stopped
at the extract seam by a spy; the read path past it is tests/test_m8_integration.py's.
"""

from __future__ import annotations

import json

import pytest

from safeops.agent.output import AgentOutput, Termination
from safeops.agent.parser import ParsedProposal, ProposalParser
from safeops.domain import Proposal
from safeops.enums import ProposalStatus
from safeops.experiment.records import RunRecordWriter
from safeops.experiment.runner import (
    ExperimentRunner,
    PostAgentDiscard,
    ProviderFailure,
    _cause,
)
from safeops.faults.alerts import AlertGenerator
from tests.conftest import FakeEnvironment, FakeInjector, FakeWorkload
from tests.test_m5_records import (
    RECOVERED,
    STEP,
    UNIT,
    AgentProvenance,
    Fixed,
    Rec,
    _records,
)

S = ProposalStatus
ACTION = {
    "type": "scale_deployment",
    "target": {"kind": "Deployment", "ns": "testbed", "name": "orders"},
    "params": {"replicas": 3},
}


def _text(action=ACTION) -> str:
    return json.dumps(
        {
            "diagnosis": "orders is starved of replicas",
            "recommended_action": action,
            "confidence": 0.6,
            "evidence": [1],
            "prerequisites_claimed": [],
        }
    )


OUTPUTS = {
    S.VALID_ACTION: AgentOutput(Termination.ANSWER, _text(), 1),
    S.NO_ACTION: AgentOutput(Termination.ANSWER, _text(None), 1),
    S.INVALID_OUTPUT: AgentOutput(Termination.ANSWER, "rollback orders please", 1),
    S.STEP_LIMIT: AgentOutput(Termination.STEP_LIMIT, None, 1),
    S.MODEL_REFUSAL: AgentOutput(Termination.REFUSAL, "", 1),
    S.PROVIDER_FAILURE: AgentOutput(Termination.PROVIDER_FAILURE, None, 1),
}


class OutputAgent:
    def __init__(self, output: AgentOutput, steps) -> None:
        self.output, self.steps = output, list(steps)

    def investigate(self, context):
        return self.output, list(self.steps)


class Spy:
    """A downstream read-path or write-path component that records any call it gets."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append(name)
            raise AssertionError(f"{name} must not be reached")

        return call


def _runner(tmp_path, output: AgentOutput, steps=(STEP,), **components) -> ExperimentRunner:
    parts = {
        "extractor": Spy(),
        "shadow_gates": Spy(),
        "gate": Spy(),
        "executor": Spy(),
        "effectiveness": Fixed(RECOVERED),
        "harm": Fixed(Rec({"harmful": False, "harm_violations": []})),
    }
    parts.update(components)
    return ExperimentRunner(
        environment=FakeEnvironment(),
        workload=FakeWorkload(),
        injector=FakeInjector(),
        alerts=AlertGenerator(namespace="testbed"),
        agent=OutputAgent(output, steps),
        parser=ProposalParser(),
        records=RunRecordWriter(runs_dir=tmp_path / "runs", provenance=AgentProvenance()),
        **parts,
    )


def _route(status: ProposalStatus):
    parsed = ProposalParser().parse(OUTPUTS[status])
    assert parsed.status is status  # reached from a real envelope, never constructed
    runner = ExperimentRunner.__new__(ExperimentRunner)
    return runner._gate_eligible_action(parsed)


# --- the routing hook, for every status -------------------------------------------------------


def test_valid_action_routes_its_action() -> None:
    action = _route(S.VALID_ACTION)
    assert action is not None and action.model_dump(mode="json") == ACTION


@pytest.mark.parametrize(
    "status", [S.NO_ACTION, S.INVALID_OUTPUT, S.STEP_LIMIT, S.MODEL_REFUSAL], ids=str
)
def test_agent_outcomes_route_to_no_action(status) -> None:
    assert _route(status) is None


def test_provider_failure_raises_its_routing_signal() -> None:
    with pytest.raises(ProviderFailure):
        _route(S.PROVIDER_FAILURE)


def test_the_provider_signal_is_cause_provider_failure() -> None:
    assert _cause(ProviderFailure("x"), {}, aborted=False) == "provider_failure"


def test_routing_takes_only_a_parsed_proposal() -> None:
    runner = ExperimentRunner.__new__(ExperimentRunner)
    with pytest.raises(TypeError):
        runner._gate_eligible_action({"proposal_status": "valid_action"})


def test_every_status_has_a_route() -> None:
    assert set(OUTPUTS) == set(ProposalStatus)


# --- through the real runner -----------------------------------------------------------------


def test_provider_failure_is_a_discard_at_route_proposal(tmp_path) -> None:
    r = _runner(tmp_path, OUTPUTS[S.PROVIDER_FAILURE])
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["proposal_status"] == "provider_failure" and rec["proposal"] is None
    assert rec["discarded"] is True
    assert rec["discard_reason"]["stage"] == "route_proposal"
    assert rec["discard_reason"]["cause"] == "provider_failure"
    assert rec["trajectory"] and rec["outcome"] is None and rec["feature_vector"] is None
    for name in ("extractor", "shadow_gates", "gate", "executor"):
        assert getattr(r, name).calls == [], name


def test_valid_action_routes_on_to_the_extract_seam(tmp_path) -> None:
    """valid_action goes past route_proposal into extract, which receives the domain
    Proposal (decision 0025 D§5), never the parser's ParsedProposal. The spy stops the run
    there, so nothing further down the read path is faked."""
    extractor = _SpyExtractor()
    r = _runner(tmp_path, OUTPUTS[S.VALID_ACTION], extractor=extractor)
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    (proposal, trajectory) = extractor.seen
    assert type(proposal) is Proposal and proposal.recommended_action.model_dump(mode="json") == (
        ACTION
    )
    assert len(trajectory) == 1
    (rec,) = _records(tmp_path)
    assert rec["proposal_status"] == "valid_action"
    assert rec["discard_reason"]["stage"] == "extract"  # past route_proposal
    assert "extract reached" in rec["discard_reason"]["error"]


class _SpyExtractor:
    def __init__(self) -> None:
        self.seen = None

    def extract(self, proposal, trajectory, context):
        self.seen = (proposal, trajectory)
        raise RuntimeError("extract reached")


@pytest.mark.parametrize(
    "status", [S.NO_ACTION, S.INVALID_OUTPUT, S.STEP_LIMIT, S.MODEL_REFUSAL], ids=str
)
def test_agent_outcomes_are_retained_not_discarded(tmp_path, status) -> None:
    r = _runner(tmp_path, OUTPUTS[status])
    r.run(UNIT)  # stabilization, snapshot, effectiveness, harm(None): a completed record
    (rec,) = _records(tmp_path)
    assert rec["proposal_status"] == status.value
    assert rec["discarded"] is False and rec["discard_reason"] is None
    assert rec["gate"] is None and rec["execution"] is None and rec["in_cohort_g"] is False
    for name in ("extractor", "shadow_gates", "gate", "executor"):
        assert getattr(r, name).calls == [], name


# --- the trajectory-count guard, at stage parse -----------------------------------------------


@pytest.mark.parametrize(
    ("declared", "steps"),
    [(1, ()), (0, (STEP,))],
    ids=["declared-1-actual-0", "declared-0-actual-1"],
)
def test_a_trajectory_count_mismatch_is_discarded_at_parse(tmp_path, declared, steps) -> None:
    output = AgentOutput(Termination.ANSWER, _text(), declared)
    r = _runner(tmp_path, output, steps=steps)
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["discard_reason"]["stage"] == "parse"  # never "agent"
    assert rec["discard_reason"]["cause"] == "infrastructure_failure"
    assert "TrajectoryCountMismatch" in rec["discard_reason"]["error"]
    assert len(rec["trajectory"]) == len(steps)  # the recorded steps are kept
    assert rec["proposal_status"] is None  # the parser never ran


def test_a_matching_trajectory_count_proceeds(tmp_path) -> None:
    r = _runner(tmp_path, AgentOutput(Termination.ANSWER, _text(None), 1))
    r.run(UNIT)
    (rec,) = _records(tmp_path)
    assert rec["proposal_status"] == "no_action" and rec["discarded"] is False


def test_the_count_is_checked_before_the_parser_runs(tmp_path) -> None:
    class CountingParser(ProposalParser):
        calls = 0

        def parse(self, raw_output) -> ParsedProposal:
            CountingParser.calls += 1
            return super().parse(raw_output)

    r = _runner(tmp_path, AgentOutput(Termination.ANSWER, _text(), 2))
    r.parser = CountingParser()
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    assert CountingParser.calls == 0
