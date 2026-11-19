"""Loud stubs for pipeline stages that are not implemented yet.

A stub never fakes success. Calling one raises ``NotImplementedStage`` naming the
stage and the milestone (design §14) that implements it. Rule 15 forbids stubs that
return plausible values -- a ``verify_clean_state()`` that returns ``True`` would
poison every result -- not stubs that refuse to run.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

_MARKER = "__safeops_stub_milestone__"

# Milestone 1 is the scaffold itself, so a stub always points at Milestone 2..17.
_FIRST_STUB_MILESTONE = 2
_LAST_MILESTONE = 17


class NotImplementedStage(Exception):
    """A pipeline stage was called before its milestone implemented it."""

    def __init__(self, stage: str, milestone: int) -> None:
        self.stage = stage
        self.milestone = milestone
        super().__init__(f"{stage} is not implemented yet (Milestone {milestone})")


def stub(milestone: int) -> Callable[[F], F]:
    """Mark a function as an unimplemented stage that refuses to run."""
    if not _FIRST_STUB_MILESTONE <= milestone <= _LAST_MILESTONE:
        raise ValueError(
            f"stub milestone must be {_FIRST_STUB_MILESTONE}..{_LAST_MILESTONE}, got {milestone}"
        )

    def decorate(fn: F) -> F:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def refuse(*args: Any, **kwargs: Any) -> Any:
            # A call that doesn't match the declared signature is a wiring bug. Raise
            # TypeError for it, the way a real implementation would, instead of hiding it
            # behind "not implemented yet".
            signature.bind(*args, **kwargs)
            raise NotImplementedStage(fn.__qualname__, milestone)

        setattr(refuse, _MARKER, milestone)
        return refuse  # type: ignore[return-value]

    return decorate


def stub_milestone(fn: object) -> int | None:
    """Milestone a stub is waiting on, or ``None`` if ``fn`` is implemented."""
    return getattr(fn, _MARKER, None)
