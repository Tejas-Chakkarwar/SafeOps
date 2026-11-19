"""Agent loop: observe, diagnose, propose (design §11.4). Implemented at Milestone 9."""

from __future__ import annotations

from safeops.stubs import stub


class Agent:
    @stub(milestone=9)
    def investigate(self, context):
        """Investigate with read-only tools; return (raw_model_output, trajectory).

        Receives IncidentRuntimeContext only (§6). Step cap 15.
        """
