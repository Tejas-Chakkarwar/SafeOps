"""Harm checker (design §11.7). Implemented at Milestone 7, by hand (Rule 16)."""

from __future__ import annotations

from safeops.stubs import stub


class HarmChecker:
    @stub(milestone=7)
    def check(self, action, context, postfault, postaction):
        """Return a HarmResult covering state violations (postaction vs postfault) and
        action-policy violations. ``action`` is None for no_action runs.
        """
