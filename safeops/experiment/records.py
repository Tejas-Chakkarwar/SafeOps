"""Run record writer (design §11.8). Implemented at Milestone 5."""

from __future__ import annotations

from safeops.stubs import stub


class RunRecordWriter:
    @stub(milestone=5)
    def finalize(self, **fields) -> None:
        """Validate the run record against the Pydantic model and write it once.

        Finalized records are immutable.
        """
