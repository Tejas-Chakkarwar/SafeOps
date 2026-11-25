"""The agent loop's output envelope, the proposal parser's input (decision 0025 D§3.1).

The loop or the model adapter decides how the agent finished; the parser never infers it
from text. Milestone 9's loop produces these; Milestone 8 tests fabricate them.

A malformed envelope is a harness bug (TypeError/ValueError), never `invalid_output`: it is
built by SafeOps code, not written by the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Termination(StrEnum):
    ANSWER = "answer"  # the model gave a final answer (its text may be empty)
    REFUSAL = "refusal"  # the provider adapter signalled a refusal
    STEP_LIMIT = "step_limit"  # the loop hit its step cap without a proposal
    PROVIDER_FAILURE = "provider_failure"  # the provider was unavailable after retries


_HAS_TEXT = frozenset({Termination.ANSWER, Termination.REFUSAL})


@dataclass(frozen=True)
class AgentOutput:
    termination: Termination
    text: str | None
    trajectory_steps: int  # how many trajectory steps the loop recorded

    def __post_init__(self) -> None:
        if type(self.termination) is not Termination:
            raise TypeError("AgentOutput.termination must be a Termination")
        if self.termination in _HAS_TEXT:
            if type(self.text) is not str:
                raise TypeError(f"a {self.termination} output carries its text (a str)")
        elif self.text is not None:
            raise ValueError(f"a {self.termination} output carries no text")
        if type(self.trajectory_steps) is not int:
            raise TypeError("AgentOutput.trajectory_steps must be an int (not a bool)")
        if self.trajectory_steps < 0:
            raise ValueError("AgentOutput.trajectory_steps must be non-negative")
