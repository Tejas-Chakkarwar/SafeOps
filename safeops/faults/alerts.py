"""Alert generator (design §6; decision 0015).

The one place where evaluator ground truth could leak into the agent's view. It never
receives the fault spec: its only input is the FaultVerification, which carries the
declared symptom (from the spec's `symptoms:` field) and the harness's measurements.
The Alert takes exactly the symptom's alert name, target and expression, the testbed
namespace from config/cluster.yaml, and the time the expression was measured to hold.

An alert is built only for a symptom that held, so its text is a true statement about
the cluster at `fired_at`, not a declaration from the spec.
"""

from __future__ import annotations

from safeops.context import Alert
from safeops.faults.injector import FaultVerification


class AlertGenerator:
    def __init__(self, namespace: str | None = None) -> None:
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        if self._namespace is None:
            from safeops.cluster import load_cluster_config

            self._namespace = load_cluster_config().namespace
        return self._namespace

    def build_alert(self, verification: FaultVerification) -> Alert:
        """Build an Alert from the verified symptom (spec ``symptoms:``) and nothing else."""
        if not isinstance(verification, FaultVerification):
            raise TypeError("build_alert takes a FaultVerification, never a fault spec")
        held = verification.held_observation
        if not verification.present or held is None:
            raise ValueError("no alert for a symptom that was not measured to hold")
        symptom = verification.symptom
        return Alert(
            alert_name=symptom.alert_name,
            namespace=self.namespace,
            target_service=symptom.target_service,
            symptom=symptom.expression,
            fired_at=held.observed_at,
        )
