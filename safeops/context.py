"""What the SafeOps runtime is allowed to know about an incident (design §6).

This module is deliberately neutral. The agent, the feature extractor and the gate all
consume these types, so they must not live under ``safeops.faults``: defining them there
would force those packages to import the package that holds evaluator ground truth, and
the information-wall test would (correctly) fail (decision 0004).

Nothing here may import ``safeops.faults``. tests/test_information_wall.py checks this
module along with the agent, features and gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safeops.topology import ServiceTopology


@dataclass(frozen=True)
class Alert:
    """What a real Alertmanager webhook would carry. Nothing more."""

    alert_name: str
    namespace: str
    target_service: str
    symptom: str
    fired_at: datetime

    def __post_init__(self) -> None:
        for name in ("alert_name", "namespace", "target_service", "symptom"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"Alert.{name} must be a non-empty string")
        if self.fired_at.tzinfo is None:
            raise ValueError("Alert.fired_at must be timezone-aware")

    def to_dict(self) -> dict:
        return {
            "alert_name": self.alert_name,
            "namespace": self.namespace,
            "target_service": self.target_service,
            "symptom": self.symptom,
            "fired_at": self.fired_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True)
class IncidentRuntimeContext:
    """Everything the agent, the extractor and the gate receive. It holds no reference to
    any fault spec, by construction: an Alert, the public topology, and the time."""

    alert: Alert
    topology: ServiceTopology  # config/topology.yaml: static and public
    now: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.alert, Alert):
            raise TypeError("IncidentRuntimeContext.alert must be an Alert")
        if not isinstance(self.topology, ServiceTopology):
            raise TypeError("IncidentRuntimeContext.topology must be a ServiceTopology")
        if self.now.tzinfo is None:
            raise ValueError("IncidentRuntimeContext.now must be timezone-aware")
        if self.alert.target_service not in self.topology.services:
            raise ValueError(f"alert targets unknown service {self.alert.target_service!r}")
