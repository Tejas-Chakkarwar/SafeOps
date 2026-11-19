"""Cohort builder and offline gate replay (design §7.2, §10.4). Implemented at Milestone 13."""

from __future__ import annotations

from safeops.stubs import stub


@stub(milestone=13)
def replay_cohort() -> None:
    """Build cohort G once, then replay every strategy over exactly G."""
