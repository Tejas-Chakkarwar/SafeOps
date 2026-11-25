"""Milestone 8A-2 through the real runner (decision 0025 D§1, D§4, D§6, D§7.1, D§16).

The real parser, extractor, shadow scorer and always-act gate, sharing one loaded policy,
fed a fabricated AgentOutput and stored trajectory (Rule 17); the harness around them is
the offline fakes. The executor is still Milestone 8B's stub, so an always-act valid action
stops there, recorded with every read-path value it produced.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import typing

import pytest

from safeops.agent.output import AgentOutput, Termination
from safeops.agent.parser import ProposalParser
from safeops.domain import FeatureVector, ShadowGates
from safeops.executor.executor import ActionExecutor
from safeops.experiment import runner as runner_module
from safeops.experiment.consistency import ReadPathCheck
from safeops.experiment.records import RunRecordWriter
from safeops.experiment.runner import ExperimentRunner, PostAgentDiscard
from safeops.experiment.schema import RecordValidationError, validate_record_json
from safeops.faults.alerts import AlertGenerator
from safeops.features.extractor import RiskFeatureExtractor
from safeops.features.policy import load_policy
from safeops.gate.always_act import AlwaysActGate
from safeops.gate.protocol import Gate, GateDecision
from safeops.gate.shadow import ShadowGateScorer, random_draw
from safeops.stubs import NotImplementedStage, stub_milestone
from tests.conftest import FakeEnvironment, FakeInjector, FakeWorkload
from tests.m8_fixtures import deployment, ok, step
from tests.test_m5_records import RECOVERED, SHA, UNIT, AgentProvenance, Fixed, Rec

POLICY = load_policy()
ACTION = {
    "type": "restart_deployment",
    "target": {"kind": "Deployment", "ns": "testbed", "name": "orders"},
    "params": {},
}
TRAJECTORY = [step(1, "get_deployment", "orders", ok(deployment("orders", 2)))]


def _text(action=ACTION, claims=()) -> str:
    return json.dumps(
        {
            "diagnosis": "orders is wedged",
            "recommended_action": action,
            "confidence": 0.75,
            "evidence": [1],
            "prerequisites_claimed": list(claims),
        }
    )


class OutputAgent:
    def __init__(self, text: str = _text(), trajectory=TRAJECTORY) -> None:
        self.text, self.trajectory = text, list(trajectory)

    def investigate(self, context):
        return AgentOutput(Termination.ANSWER, self.text, len(self.trajectory)), self.trajectory


class Spy:
    """Wraps a real component and records the calls it gets."""

    def __init__(self, inner=None, raises: Exception | None = None) -> None:
        self.inner, self.raises, self.calls = inner, raises, []

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append((name, a, k))
            if self.raises is not None:
                raise self.raises
            return getattr(self.inner, name)(*a, **k)

        return call


class RecordingWriter(RunRecordWriter):
    """The seam that stops before production persistence: it keeps the fields."""

    def __init__(self) -> None:
        super().__init__(provenance=AgentProvenance())
        self.fields = None

    def finalize(self, runtime_context=None, **fields):
        self.fields = fields
        return None


class CorruptingWriter(RunRecordWriter):
    """The real writer and its read-path check, with one value changed on its way in."""

    def __init__(self, runs_dir, read_path, corrupt=None) -> None:
        super().__init__(runs_dir=runs_dir, provenance=AgentProvenance(), read_path=read_path)
        self.corrupt = corrupt

    def finalize(self, runtime_context=None, **fields):
        if self.corrupt is not None:
            runtime_context = self.corrupt(fields, runtime_context)
        return super().finalize(runtime_context=runtime_context, **fields)


class TracingRunner(ExperimentRunner):
    def _should_execute(self, decision):
        self.routed = getattr(self, "routed", []) + [decision]
        result = super()._should_execute(decision)
        self.routed_to = result
        return result


def _runner(tmp_path, *, agent=None, gate=None, executor=None, records=None, scorer=None):
    extractor = RiskFeatureExtractor(POLICY)
    shadow = ShadowGateScorer(POLICY)
    records = records or CorruptingWriter(tmp_path / "runs", ReadPathCheck(extractor, shadow))
    return TracingRunner(
        environment=FakeEnvironment(),
        workload=FakeWorkload(),
        injector=FakeInjector(),
        alerts=AlertGenerator(namespace="testbed"),
        agent=agent or OutputAgent(),
        parser=ProposalParser(),
        extractor=extractor,
        shadow_gates=scorer or shadow,
        gate=gate or AlwaysActGate(),
        executor=executor or ActionExecutor(),
        effectiveness=Fixed(RECOVERED),
        harm=Fixed(Rec({"harmful": False, "harm_violations": []})),
        records=records,
    )


def _written(tmp_path, sub: str = "") -> list[dict]:
    d = tmp_path / "runs" / sub if sub else tmp_path / "runs"
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else []


# --- the M8A read path, end to end ------------------------------------------------------------


def test_a_valid_action_runs_the_whole_read_path_and_stops_at_the_executor(tmp_path) -> None:
    r = _runner(tmp_path)
    with pytest.raises(NotImplementedStage) as exc:
        r.run(UNIT)
    assert exc.value.stage == "ActionExecutor.apply"
    (rec,) = _written(tmp_path)
    assert rec["discard_reason"]["stage"] == "execute"
    assert rec["discard_reason"]["cause"] == "not_implemented"
    assert rec["action_attempted"] is True and rec["execution"] is None
    assert rec["proposal_status"] == "valid_action"
    assert rec["feature_vector"] == {
        "irreversibility": 0.2,
        "blast_radius": 0.3,
        "persistence_risk": 0.0,
        "scope_outside_target": 0.0,
        "confidence_deficit": 0.25,
        "evidence_insufficiency": 0.0,
        "prerequisites_unverified": 0.0,
        "telemetry_conflict": 0.0,
    }
    assert rec["shadow_gates"] == {
        "safeops": {"risk_score": 0.09375},  # (0.2 + 0.3 + 0.25) / 8
        "confidence": {"confidence": 0.75},
        "random": {"draw": random_draw(rec["run_id"])},
    }
    assert rec["gate"] == {"strategy": "always-act", "decision": "execute", "score": None}
    assert {k: rec["provenance"][k] for k in POLICY.provenance()} == POLICY.provenance()
    assert rec["provenance"]["gate_feature_version"] == 0
    assert r.routed == [GateDecision("always-act", "execute", None)] and r.routed_to is True


def test_every_stored_value_validates_offline_too(tmp_path) -> None:
    with pytest.raises(NotImplementedStage):
        _runner(tmp_path).run(UNIT)
    (path,) = sorted((tmp_path / "runs").glob("*.json"))
    assert validate_record_json(path.read_text()).feature_vector is not None


# --- strategies swap without touching other files ------------------------------------------------


class EscalateGate:
    """Test-local: a conforming Gate that never executes. No production file knows it."""

    name = "test-escalate"

    def __init__(self) -> None:
        self.seen: list = []

    def decide(self, features: FeatureVector) -> GateDecision:
        self.seen.append(features)
        return GateDecision(strategy=self.name, decision="escalate", score=None)


def test_the_runner_depends_on_the_gate_protocol() -> None:
    assert typing.get_type_hints(ExperimentRunner.__init__)["gate"] is Gate


def test_a_test_local_gate_swaps_in_and_the_executor_is_never_called(tmp_path) -> None:
    gate, writer = EscalateGate(), RecordingWriter()
    extractor = Spy(RiskFeatureExtractor(POLICY))
    scorer = Spy(ShadowGateScorer(POLICY))
    executor = Spy(raises=AssertionError("the executor must not be called"))
    r = _runner(tmp_path, gate=gate, executor=executor, records=writer)
    r.extractor, r.shadow_gates = extractor, scorer
    r.run(UNIT)  # completes: escalated, measured with harm(None)
    assert [c[0] for c in extractor.calls] == ["extract"]
    assert [c[0] for c in scorer.calls] == ["score"]
    assert len(gate.seen) == 1 and isinstance(gate.seen[0], FeatureVector)
    assert r.routed_to is False and executor.calls == []
    f = writer.fields
    assert f["decision"] == GateDecision("test-escalate", "escalate", None)
    assert f["action_attempted"] is False and f.get("execution") is None
    assert f["features"] == gate.seen[0]


def test_the_phase_a_record_refuses_an_escalating_gate(tmp_path) -> None:
    """Why the swap test stops at a seam: version 2 represents online always-act only."""
    writer = RecordingWriter()
    _runner(tmp_path, gate=EscalateGate(), records=writer).run(UNIT)
    real = RunRecordWriter(runs_dir=tmp_path / "runs", provenance=AgentProvenance())
    text = json.dumps(real.build(**writer.fields), sort_keys=True)
    with pytest.raises(RecordValidationError, match="gate"):
        validate_record_json(text)


# --- shadows never control execution ------------------------------------------------------------


class FixedScores:
    def __init__(self, risk: float, conf: float, draw: float) -> None:
        self.value = ShadowGates(
            safeops={"risk_score": risk}, confidence={"confidence": conf}, random={"draw": draw}
        )

    def score(self, features, run_id):
        return self.value


@pytest.mark.parametrize(
    ("risk", "conf", "draw"), [(1.0, 0.0, 0.999), (0.0, 1.0, 0.0), (0.6, 0.4, 0.5)]
)
def test_shadow_values_do_not_change_the_routing(tmp_path, risk, conf, draw) -> None:
    executor = Spy(raises=RuntimeError("executor reached"))
    r = _runner(
        tmp_path,
        executor=executor,
        records=RecordingWriter(),
        scorer=FixedScores(risk, conf, draw),
    )
    with pytest.raises(PostAgentDiscard):
        r.run(UNIT)
    assert [c[0] for c in executor.calls] == ["apply"]
    assert r.routed_to is True and all(isinstance(d, GateDecision) for d in r.routed)


def test_should_execute_is_passed_only_the_gate_decision() -> None:
    tree = ast.parse(inspect.getsource(ExperimentRunner.run).lstrip().replace("\n    ", "\n"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_should_execute"
    ]
    assert len(calls) == 1
    (arg,) = calls[0].args
    assert isinstance(arg, ast.Name) and arg.id == "decision" and not calls[0].keywords
    assigns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "decision" for t in n.targets)
    ]
    (assign,) = assigns
    assert ast.unparse(assign.value) == "self.gate.decide(features)"


# --- write-time consistency (D§16): plumbing, against the stored evidence -------------------------


def _replace_features(**update):
    def corrupt(fields, ctx):
        fields["features"] = fields["features"].model_copy(update=update)
        return ctx

    return corrupt


def _replace_shadow(section: str, key: str, value: float):
    def corrupt(fields, ctx):
        dumped = fields["shadow_scores"].model_dump()
        dumped[section][key] = value
        fields["shadow_scores"] = ShadowGates(**dumped)
        return ctx

    return corrupt


def _replace_provenance(key: str, value):
    def corrupt(fields, ctx):
        fields["provenance"] = {**fields["provenance"], key: value}
        return ctx

    return corrupt


def _replace_trajectory(fields, ctx):
    # The same stored representation a record holds: orders now shows 7 replicas.
    fields["trajectory"] = [step(1, "get_deployment", "orders", ok(deployment("orders", 7)))]
    return ctx


def _other_topology(fields, ctx):
    """A runtime context that isn't the one extraction used: an extra caller of gateway
    rescales every dependent component. (The stored alert itself can't be swapped this way:
    the schema already pins it to the verified symptom.)"""
    import yaml

    from safeops.topology import TOPOLOGY_PATH, parse_topology

    data = yaml.safe_load(TOPOLOGY_PATH.read_text())
    data["services"]["edge"] = {"depends_on": ["gateway"]}
    return type(ctx)(alert=ctx.alert, topology=parse_topology(data), now=ctx.now)


def _replace_claims(fields, ctx):
    parsed = fields["proposal"]
    proposal = parsed.proposal.model_copy(
        update={"prerequisites_claimed": ["prior_revision_healthy"]}
    )
    fields["proposal"] = type(parsed)(parsed.status, proposal)
    return ctx


def _drop_context(fields, ctx):
    return None


CORRUPTIONS = {
    "blast-radius": (_replace_features(blast_radius=0.31), "feature_vector.blast_radius"),
    "evidence-insufficiency": (
        _replace_features(evidence_insufficiency=0.5),
        "feature_vector.evidence_insufficiency",
    ),
    "risk-score": (_replace_shadow("safeops", "risk_score", 0.1), "shadow_gates.safeops"),
    "draw": (_replace_shadow("random", "draw", 0.5), "shadow_gates.random.draw"),
    "conflict-rules-hash": (
        _replace_provenance("conflict_rules_hash", SHA),
        "provenance.conflict_rules_hash",
    ),
    "action-semantics-hash": (
        _replace_provenance("action_semantics_hash", SHA),
        "provenance.action_semantics_hash",
    ),
    "gate-feature-version": (
        _replace_provenance("gate_feature_version", 1),
        "provenance.gate_feature_version",
    ),
    "stored-trajectory": (_replace_trajectory, "feature_vector.blast_radius"),
    "runtime-topology": (_other_topology, "feature_vector.blast_radius"),
    "stored-proposal": (_replace_claims, "feature_vector.prerequisites_unverified"),
    "no-runtime-context": (_drop_context, "no runtime context"),
}


def test_the_uncorrupted_record_passes_the_write_time_check(tmp_path) -> None:
    with pytest.raises(NotImplementedStage):
        _runner(tmp_path).run(UNIT)
    assert len(_written(tmp_path)) == 1 and _written(tmp_path, "rejected") == []


@pytest.mark.parametrize(("corrupt", "problem"), CORRUPTIONS.values(), ids=CORRUPTIONS.keys())
def test_a_clobbered_value_or_mismatched_evidence_is_refused(tmp_path, corrupt, problem) -> None:
    extractor, scorer = RiskFeatureExtractor(POLICY), ShadowGateScorer(POLICY)
    writer = CorruptingWriter(tmp_path / "runs", ReadPathCheck(extractor, scorer), corrupt)
    r = _runner(tmp_path, records=writer)
    with pytest.raises(RecordValidationError, match="writer bug") as exc:
        r.run(UNIT)
    assert problem in str(exc.value)
    assert _written(tmp_path) == []  # never in the dataset
    assert len(_written(tmp_path, "rejected")) == 1


def test_the_read_path_check_needs_one_shared_policy() -> None:
    with pytest.raises(ValueError, match="share one policy"):
        ReadPathCheck(RiskFeatureExtractor(POLICY), ShadowGateScorer(load_policy()))
    with pytest.raises(TypeError):
        ReadPathCheck(object(), ShadowGateScorer(POLICY))


# --- offline validation is configuration-free ----------------------------------------------------


def _a_record(tmp_path) -> str:
    with pytest.raises(NotImplementedStage):
        _runner(tmp_path).run(UNIT)
    (path,) = sorted((tmp_path / "runs").glob("*.json"))
    return path.read_text()


def test_offline_validation_reads_no_policy(tmp_path, monkeypatch) -> None:
    text = _a_record(tmp_path)
    import safeops.policy

    def forbidden(*a, **k):
        raise AssertionError("offline validation read a policy file")

    monkeypatch.setattr(safeops.policy, "read_policy_file", forbidden)
    monkeypatch.setattr(safeops.policy.Path, "read_bytes", forbidden)
    raw = json.loads(text)
    # Another policy condition's provenance: a historical record stays valid.
    raw["provenance"].update(
        action_semantics_hash=SHA,
        evidence_requirements_hash=SHA,
        conflict_rules_hash=SHA,
        gate_feature_version=3,
    )
    assert validate_record_json(json.dumps(raw)).provenance.gate_feature_version == 3


def test_offline_validation_imports_no_policy_or_feature_code(tmp_path) -> None:
    record = tmp_path / "record.json"
    record.write_text(_a_record(tmp_path))
    probe = (
        "import sys; from pathlib import Path;"
        "from safeops.experiment.schema import validate_record_json;"
        f"validate_record_json(Path({str(record)!r}).read_text());"
        "print(sorted(m for m in sys.modules if m in ('safeops.policy',"
        " 'safeops.features.policy', 'safeops.features.extractor',"
        " 'safeops.experiment.consistency', 'safeops.gate.shadow')))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_a_feature_vector_requires_action_semantics_hash(tmp_path) -> None:
    raw = json.loads(_a_record(tmp_path))
    raw["provenance"]["action_semantics_hash"] = None
    assert raw["execution"] is None  # so the execution-side rule can't be what refuses it
    with pytest.raises(RecordValidationError, match="feature vector exists but action_semantics"):
        validate_record_json(json.dumps(raw))


# --- build_runner shares one policy; the stubs that remain ---------------------------------------


def test_build_runner_shares_one_loaded_policy() -> None:
    r = runner_module.build_runner()
    check = r.records.read_path
    assert isinstance(check, ReadPathCheck)
    assert check.extractor is r.extractor and check.scorer is r.shadow_gates
    assert r.extractor.policy is r.shadow_gates.policy is check.policy
    assert check.provenance() == load_policy().provenance()
    assert isinstance(r.gate, AlwaysActGate)
    assert r.injector._workload is r.workload is r.effectiveness.workload


def test_the_writers_provenance_comes_from_the_loaded_policy(tmp_path) -> None:
    extractor, scorer = RiskFeatureExtractor(POLICY), ShadowGateScorer(POLICY)
    writer = RunRecordWriter(
        runs_dir=tmp_path, provenance=AgentProvenance(), read_path=ReadPathCheck(extractor, scorer)
    )
    captured = writer.capture_provenance(UNIT)
    assert {k: captured[k] for k in POLICY.provenance()} == POLICY.provenance()
    plain = RunRecordWriter(runs_dir=tmp_path, provenance=AgentProvenance())
    assert plain.capture_provenance(UNIT)["gate_feature_version"] == 1  # AgentProvenance's


def test_m8b_and_m9_stubs_remain() -> None:
    assert stub_milestone(ExperimentRunner._executed_action) == 8
    assert stub_milestone(ActionExecutor.apply) == 8
    assert stub_milestone(ExperimentRunner._should_execute) is None
    from safeops.agent.loop import Agent
    from safeops.experiment.sequence import first_stub

    assert stub_milestone(Agent.investigate) == 9
    assert first_stub().qualname == "Agent.investigate"


# --- review fixes: one evidence form; a readable policy failure ---------------------------


def test_a_live_non_json_value_is_recorded_as_extraction_saw_it(tmp_path) -> None:
    """The run extracts from the stored form, so the write-time recomputation agrees: the
    record is written, never rejected for the tuple/list difference."""
    from safeops.domain import TrajectoryStep
    from safeops.tools.canonical import result_digest
    from tests.m8_fixtures import history, revision

    plain = history("orders", [revision(1, 2, 2)])
    live_result = {"status": "ok", "data": {**plain, "revisions": tuple(plain["revisions"])}}
    live = TrajectoryStep(
        step=1,
        tool="get_rollout_history",
        args={"namespace": "testbed", "name": "orders"},
        result=live_result,
        result_digest=result_digest({"status": "ok", "data": plain}),
        tokens_in=1,
        tokens_out=1,
        latency_ms=1.0,
    )
    rollback = {**ACTION, "type": "rollback_deployment", "params": {"to_revision": 1}}
    agent = OutputAgent(_text(rollback, claims=["prior_revision_healthy"]), [live])
    with pytest.raises(NotImplementedStage):
        _runner(tmp_path, agent=agent).run(UNIT)
    assert _written(tmp_path, "rejected") == []
    (rec,) = _written(tmp_path)
    assert rec["feature_vector"]["evidence_insufficiency"] == 1 / 3
    assert rec["feature_vector"]["prerequisites_unverified"] == 0.0


def test_a_policy_file_that_cannot_load_is_a_readable_usage_error(tmp_path, monkeypatch) -> None:
    import shutil

    from typer.testing import CliRunner

    from safeops import cli
    from safeops.policy import CONFIG_DIR

    for f in CONFIG_DIR.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    weights = tmp_path / "gate_weights.yaml"
    weights.write_text(weights.read_text().replace('blast_radius: "0.125"', "blast_radius: 0.125"))
    monkeypatch.setattr(runner_module, "load_policy", lambda: load_policy(tmp_path))
    result = CliRunner().invoke(
        cli.app, ["run", "--fault", "bad-deployment-config", "--seed", "0", "--trial", "0"]
    )
    assert result.exit_code == cli.EXIT_USAGE
    assert "policy configuration: gate_weights.yaml" in result.stderr
    assert "Traceback" not in result.output + result.stderr
