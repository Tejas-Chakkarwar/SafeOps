"""Milestone 8A-2: the Gate protocol, GateDecision, always-act, _should_execute and the
shadow scalars (decision 0025 D§4, D§6). Literal expected values (Rule 17)."""

from __future__ import annotations

import math

import pytest

from safeops.domain import FeatureVector, ShadowGates
from safeops.experiment.runner import ExperimentRunner
from safeops.features.policy import FEATURE_NAMES, load_policy
from safeops.gate.always_act import AlwaysActGate
from safeops.gate.protocol import GateDecision
from safeops.gate.shadow import ShadowGateScorer, confidence, random_draw, risk_score

POLICY = load_policy()
SCORER = ShadowGateScorer(POLICY)
RUN_ID = "0373adae-6334-41ef-a0f4-6d2d5a4fc04c"


def vector(**values: float) -> FeatureVector:
    return FeatureVector(**{name: values.get(name, 0.0) for name in FEATURE_NAMES})


# --- GateDecision ---------------------------------------------------------------------------


def test_a_gate_decision_is_frozen_and_records_three_fields() -> None:
    d = GateDecision("always-act", "execute", None)
    assert d.to_record() == {"strategy": "always-act", "decision": "execute", "score": None}
    assert GateDecision("t", "escalate", 0.4).to_record()["score"] == 0.4
    with pytest.raises(AttributeError):
        d.decision = "escalate"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("args", "exc"),
    [
        (("always-act", "run", None), ValueError),
        (("always-act", "EXECUTE", None), ValueError),
        (("always-act", True, None), ValueError),
        (("", "execute", None), TypeError),
        ((None, "execute", None), TypeError),
        (("t", "execute", float("nan")), ValueError),
        (("t", "execute", float("inf")), ValueError),
        (("t", "execute", 1), ValueError),
        (("t", "execute", "0.5"), ValueError),
    ],
)
def test_an_invalid_gate_decision_is_refused(args, exc) -> None:
    with pytest.raises(exc):
        GateDecision(*args)


# --- always-act -----------------------------------------------------------------------------

EXTREMES = [
    vector(),
    vector(**{n: 1.0 for n in FEATURE_NAMES}),
    vector(blast_radius=1.0, persistence_risk=1.0),
    vector(confidence_deficit=1.0, evidence_insufficiency=1.0),
    vector(telemetry_conflict=1.0),
    vector(irreversibility=0.6, scope_outside_target=1.0, prerequisites_unverified=0.5),
]


@pytest.mark.parametrize("features", EXTREMES, ids=range(len(EXTREMES)))
def test_always_act_executes_whatever_the_features(features) -> None:
    gate = AlwaysActGate()
    assert gate.name == "always-act"
    assert gate.decide(features) == GateDecision("always-act", "execute", None)


def test_always_act_reads_no_feature_value() -> None:
    class Untouchable(FeatureVector):
        def __getattribute__(self, name):
            if name in FEATURE_NAMES:
                raise AssertionError(f"always-act read {name}")
            return super().__getattribute__(name)

    assert AlwaysActGate().decide(Untouchable(**vector().model_dump())).decision == "execute"


@pytest.mark.parametrize("bad", [None, {}, vector().model_dump(), "features"])
def test_always_act_takes_a_feature_vector_only(bad) -> None:
    with pytest.raises(TypeError):
        AlwaysActGate().decide(bad)


# --- _should_execute ------------------------------------------------------------------------


def test_should_execute_routes_on_the_gate_decision() -> None:
    runner = ExperimentRunner.__new__(ExperimentRunner)
    assert runner._should_execute(GateDecision("always-act", "execute", None)) is True
    assert runner._should_execute(GateDecision("test-escalate", "escalate", None)) is False


@pytest.mark.parametrize(
    "bad",
    [
        "execute",
        {"strategy": "always-act", "decision": "execute", "score": None},
        True,
        None,
        ShadowGates(
            safeops={"risk_score": 0.0}, confidence={"confidence": 1.0}, random={"draw": 0.0}
        ),
    ],
    ids=["string", "dict", "bool", "none", "shadow-gates"],
)
def test_should_execute_takes_a_gate_decision_only(bad) -> None:
    runner = ExperimentRunner.__new__(ExperimentRunner)
    with pytest.raises(TypeError):
        runner._should_execute(bad)


# --- shadow scalars -------------------------------------------------------------------------


def test_risk_score_literals() -> None:
    w = POLICY.gate_weights
    assert risk_score(vector(), w) == 0.0
    assert risk_score(vector(**{n: 1.0 for n in FEATURE_NAMES}), w) == 1.0
    for name in FEATURE_NAMES:
        assert risk_score(vector(**{name: 1.0}), w) == 0.125, name
    mixed = vector(
        irreversibility=0.5,
        blast_radius=0.25,
        scope_outside_target=1.0,
        confidence_deficit=0.75,
        evidence_insufficiency=0.5,
    )
    assert risk_score(mixed, w) == 0.375  # (0.5 + 0.25 + 1 + 0.75 + 0.5) / 8
    # Exact binary values: Fraction(0.2) + Fraction(0.3) is exactly 1/2, so this is 1/16.
    assert risk_score(vector(irreversibility=0.2, blast_radius=0.3), w) == 0.0625
    thirds = vector(evidence_insufficiency=1 / 3, prerequisites_unverified=2 / 3)
    assert risk_score(thirds, w) == 0.125


def test_risk_score_uses_every_feature_with_its_own_weight() -> None:
    from dataclasses import replace
    from fractions import Fraction

    from safeops.features.policy import GateWeights

    names = list(FEATURE_NAMES)
    skewed = GateWeights(0, {n: Fraction(i + 1, 36) for i, n in enumerate(names)})
    assert sum(skewed.weights.values()) == 1
    for i, name in enumerate(names):
        assert risk_score(vector(**{name: 1.0}), skewed) == float(Fraction(i + 1, 36)), name
    scorer = ShadowGateScorer(replace(POLICY, gate_weights=skewed))
    assert scorer.score(vector(telemetry_conflict=1.0), RUN_ID).safeops.risk_score == 8 / 36


def test_confidence_shadow_is_one_minus_the_deficit() -> None:
    assert confidence(vector(confidence_deficit=0.0)) == 1.0
    assert confidence(vector(confidence_deficit=1.0)) == 0.0
    assert confidence(vector(confidence_deficit=0.25)) == 0.75
    deficit = 1.0 - 0.82
    assert math.isclose(confidence(vector(confidence_deficit=deficit)), 0.82, abs_tol=1e-12)


RANDOM_VECTORS = {
    "0373adae-6334-41ef-a0f4-6d2d5a4fc04c": 0.1508529361823886,
    "00000000-0000-4000-8000-000000000000": 0.1529875729041179,
    "ffffffff-ffff-4fff-bfff-ffffffffffff": 0.036934448477598636,
}


@pytest.mark.parametrize(("run_id", "draw"), RANDOM_VECTORS.items())
def test_random_draw_literals(run_id, draw) -> None:
    assert random_draw(run_id) == draw
    assert random_draw(run_id) == random_draw(run_id)
    assert 0.0 <= draw < 1.0


@pytest.mark.parametrize(
    "bad",
    [
        "0373ADAE-6334-41EF-A0F4-6D2D5A4FC04C",
        "0373adae633441efa0f46d2d5a4fc04c",
        "0373adae-6334-11ef-a0f4-6d2d5a4fc04c",  # not version 4
        "",
        None,
        b"0373adae-6334-41ef-a0f4-6d2d5a4fc04c",
    ],
)
def test_random_draw_refuses_anything_but_the_records_run_id(bad) -> None:
    with pytest.raises(ValueError):
        random_draw(bad)


def test_the_scorer_returns_the_three_scalars() -> None:
    fv = vector(blast_radius=1.0, confidence_deficit=0.25)
    got = SCORER.score(fv, run_id=RUN_ID)
    assert got == ShadowGates(
        safeops={"risk_score": 0.15625},
        confidence={"confidence": 0.75},
        random={"draw": 0.1508529361823886},
    )
    with pytest.raises(TypeError):
        SCORER.score(fv.model_dump(), run_id=RUN_ID)
    with pytest.raises(TypeError):
        ShadowGateScorer(None)
