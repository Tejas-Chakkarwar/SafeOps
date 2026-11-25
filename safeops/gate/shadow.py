"""Shadow gate scalars recorded during Phase A (design §10.4, §11.8; decision 0025 D§6).

One deterministic scalar per replayed strategy, each its own pure function. None of them
affects execution: the runner routes on the live gate's GateDecision only.

    safeops.risk_score     sum(w_i * f_i): the weights' exact decimals times each stored
                           feature float's exact binary value (Fraction(f)), summed exactly,
                           converted to float once
    confidence.confidence  1.0 - confidence_deficit, in float arithmetic
    random.draw            (first 8 bytes of sha256(b"safeops/random-p/v1:" + run_id),
                           big-endian, >> 11) / 2**53: in [0, 1 - 2**-53], no RNG (Rule 24)
"""

from __future__ import annotations

import hashlib
import re
from fractions import Fraction

from safeops.domain import FeatureVector, ShadowGates
from safeops.features.policy import FEATURE_NAMES, GateWeights, PolicyBundle

RANDOM_DOMAIN = b"safeops/random-p/v1:"
# The record's run_id: a lowercase, hyphenated UUID4 (the schema's pattern).
RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def risk_score(features: FeatureVector, weights: GateWeights) -> float:
    total = sum(
        (weights.weights[name] * Fraction(getattr(features, name)) for name in FEATURE_NAMES),
        Fraction(0),
    )
    return float(total)


def confidence(features: FeatureVector) -> float:
    return 1.0 - features.confidence_deficit


def random_draw(run_id: str) -> float:
    if type(run_id) is not str or RUN_ID.fullmatch(run_id) is None:
        raise ValueError(f"run_id must be a lowercase hyphenated UUID4, got {run_id!r}")
    digest = hashlib.sha256(RANDOM_DOMAIN + run_id.encode("ascii")).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / 2**53


class ShadowGateScorer:
    def __init__(self, policy: PolicyBundle) -> None:
        if not isinstance(policy, PolicyBundle):
            raise TypeError(f"ShadowGateScorer needs a PolicyBundle, not {type(policy)}")
        self.policy = policy

    def score(self, features, run_id: str) -> ShadowGates:
        """Return one scalar per strategy: safeops risk_score, confidence, and the random draw.

        The random draw is seeded from ``run_id`` (Rule 24), which the runner allocates once
        at the start of the run and passes here (decision 0018, M5 review item 1). No scalar
        affects execution.
        """
        if not isinstance(features, FeatureVector):
            raise TypeError(f"shadow scoring takes a FeatureVector, not {type(features)}")
        return ShadowGates(
            safeops={"risk_score": risk_score(features, self.policy.gate_weights)},
            confidence={"confidence": confidence(features)},
            random={"draw": random_draw(run_id)},
        )
