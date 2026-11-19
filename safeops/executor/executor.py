"""Action executor (design §8, §9.2, §11.6). Implemented at Milestone 8.

Enforces the hard envelope only. Never add an invariant check here (Rule 14).
"""

from __future__ import annotations

from safeops.stubs import stub


class ActionExecutor:
    @stub(milestone=8)
    def apply(self, action):
        """Apply a gate-approved action as safeops-executor; return an ExecutionResult."""
