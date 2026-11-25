"""Write-time consistency of the read path's derived values (decision 0025 D§16; CLAUDE.md
evidence-consistency invariant).

Before a record is written, the writer re-derives every Milestone 8 read-path value from the
record's own stored evidence (the stored proposal, the stored trajectory with its JSON-safe
tool results, the stored alert, and run_id) under the exact runtime topology and the exact
policy objects the run used, and compares:

- the eight feature values, the safeops risk_score, the confidence scalar and the draw;
- the four policy-provenance values against the loaded policy.

**A plumbing check, not a second formula engine.** It calls the same RiskFeatureExtractor
and ShadowGateScorer instances the runner used; there is no independent restatement of any
feature here. So it catches a value clobbered after it was computed, evidence that differs
from what extraction saw, a wrong hash or version attached, a draw tied to another run id.
It does not prove a formula right: if the extractor's formula were wrong, both sides would
agree. Formula correctness is pinned where the formulas live, by literal tests and mutants
(tests/test_m8_features.py, tests/test_m8_gate.py).

Offline validation (schema.record_problems) never uses this: it has no policy and opens no
configuration, so a stored record never becomes invalid because a policy file changed later.
"""

from __future__ import annotations

from safeops.context import Alert, IncidentRuntimeContext
from safeops.features.extractor import RiskFeatureExtractor
from safeops.features.policy import FEATURE_NAMES, PolicyBundle
from safeops.gate.shadow import ShadowGateScorer


class ReadPathCheck:
    """The runtime-only validation context handed to RunRecordWriter (never persisted)."""

    def __init__(self, extractor: RiskFeatureExtractor, scorer: ShadowGateScorer) -> None:
        if not isinstance(extractor, RiskFeatureExtractor):
            raise TypeError("ReadPathCheck needs the runner's RiskFeatureExtractor")
        if not isinstance(scorer, ShadowGateScorer):
            raise TypeError("ReadPathCheck needs the runner's ShadowGateScorer")
        if extractor.policy is not scorer.policy:
            raise ValueError("the extractor and the shadow scorer must share one policy")
        self.extractor = extractor
        self.scorer = scorer

    @property
    def policy(self) -> PolicyBundle:
        return self.extractor.policy

    def provenance(self) -> dict:
        return self.policy.provenance()

    def problems(self, record, runtime_context) -> list[str]:
        """What disagrees between `record` (validated, as it will be written) and a
        recomputation from its stored evidence."""
        p = [
            f"provenance.{key} is {getattr(record.provenance, key)!r}, the loaded policy's is"
            f" {value!r}"
            for key, value in self.provenance().items()
            if getattr(record.provenance, key) != value
        ]
        fv = record.feature_vector
        if fv is None:
            return p
        if not isinstance(runtime_context, IncidentRuntimeContext):
            return p + ["a feature vector, but no runtime context to recompute it against"]
        a = record.alert
        try:
            context = IncidentRuntimeContext(
                alert=Alert(a.alert_name, a.namespace, a.target_service, a.symptom, a.fired_at),
                topology=runtime_context.topology,
                now=runtime_context.now,
            )
        except (TypeError, ValueError) as exc:
            return p + [f"the stored alert does not form a runtime context: {exc}"]
        recomputed = self.extractor.extract(record.proposal, list(record.trajectory), context)
        for name in FEATURE_NAMES:
            if getattr(fv, name) != getattr(recomputed, name):
                p.append(
                    f"feature_vector.{name} is {getattr(fv, name)!r}, the stored evidence gives"
                    f" {getattr(recomputed, name)!r}"
                )
        sg = record.shadow_gates
        if sg is not None:
            expected = self.scorer.score(fv, run_id=record.run_id)
            for label, stored, derived in (
                ("safeops.risk_score", sg.safeops.risk_score, expected.safeops.risk_score),
                ("confidence.confidence", sg.confidence.confidence, expected.confidence.confidence),
                ("random.draw", sg.random.draw, expected.random.draw),
            ):
                if stored != derived:
                    p.append(f"shadow_gates.{label} is {stored!r}, the record gives {derived!r}")
        return p
