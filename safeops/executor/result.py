"""What one execution did (decision 0025 D§11.4, D§11.5): neutral, frozen runtime types.

The executor's own types, not the run-record schema's (safeops.experiment is behind the
information wall). `to_record()` is the record form the writer stores, which the schema
validates again as `Execution` / `ExecutionStep`.

The constructors enforce the producer shapes of D§11.5, so a result that misstates what
was attempted can't be built:

    step applied / skipped      error None
    step failed                 a bounded, non-empty error
    APPLIED                     >= 1 step, every step applied, error None
    PARTIALLY_APPLIED           [patch_configmap applied, restart_deployment failed|skipped]
    REFUSED_BY_ENVELOPE         no steps, a bounded reason
    REJECTED_INVALID_ACTION     exactly the one attempted step, failed; a bounded reason
    INFRASTRUCTURE_FAILURE      no step, or exactly one failed step; a bounded reason

No step is ever manufactured to make a result look complete. Which operations a complete
sequence holds depends on the action, so the executor checks that against its loaded
action semantics (ActionExecutor._conforming).
"""

from __future__ import annotations

from dataclasses import dataclass

from safeops.domain import MAX_DISCARD_ERROR
from safeops.enums import ActionType, ExecutionStatus

APPLIED = "applied"
FAILED = "failed"
SKIPPED = "skipped"
STEP_STATUSES = (APPLIED, FAILED, SKIPPED)


def bounded(text: str) -> str:
    """A non-empty error text, cut to MAX_DISCARD_ERROR characters."""
    if type(text) is not str or not text:
        raise ValueError("an error text must be a non-empty string")
    return text[:MAX_DISCARD_ERROR]


def _check_error(error: object, where: str) -> None:
    if type(error) is not str or not error or len(error) > MAX_DISCARD_ERROR:
        raise ValueError(f"{where} needs a non-empty error of at most {MAX_DISCARD_ERROR} chars")


@dataclass(frozen=True)
class ExecutionStepResult:
    operation: ActionType
    status: str
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ActionType):
            raise TypeError(f"a step's operation is an ActionType, not {self.operation!r}")
        if self.status not in STEP_STATUSES:
            raise ValueError(f"a step's status is one of {STEP_STATUSES}, not {self.status!r}")
        if self.status == FAILED:
            _check_error(self.error, "a failed step")
        elif self.error is not None:
            raise ValueError(f"an {self.status} step carries no error")

    def to_record(self) -> dict:
        return {"operation": self.operation.value, "status": self.status, "error": self.error}


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    error: str | None
    steps: tuple[ExecutionStepResult, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExecutionStatus):
            raise TypeError(f"status must be an ExecutionStatus, not {self.status!r}")
        if type(self.steps) is not tuple or not all(
            isinstance(s, ExecutionStepResult) for s in self.steps
        ):
            raise TypeError("steps must be a tuple of ExecutionStepResult")
        statuses = [s.status for s in self.steps]
        s = ExecutionStatus
        if self.status is s.APPLIED:
            if not statuses or any(x != APPLIED for x in statuses) or self.error is not None:
                raise ValueError("applied needs >= 1 step, every step applied, and no error")
            return
        _check_error(self.error, f"{self.status.value}")
        if self.status is s.PARTIALLY_APPLIED:
            ops = tuple(x.operation for x in self.steps)
            if (
                ops != (ActionType.PATCH_CONFIGMAP, ActionType.RESTART_DEPLOYMENT)
                or statuses[0] != APPLIED
                or statuses[1] not in (FAILED, SKIPPED)
            ):
                raise ValueError(
                    "partially_applied is exactly [patch_configmap applied,"
                    " restart_deployment failed|skipped]"
                )
        elif self.status is s.REFUSED_BY_ENVELOPE:
            if self.steps:
                raise ValueError("refused_by_envelope has no steps: nothing was attempted")
        elif self.status is s.REJECTED_INVALID_ACTION:
            if statuses != [FAILED]:
                raise ValueError("rejected_invalid_action is exactly the attempted step, failed")
        elif self.status is s.INFRASTRUCTURE_FAILURE:
            if statuses not in ([], [FAILED]):
                raise ValueError("infrastructure_failure is [] or exactly one failed step")

    def to_record(self) -> dict:
        return {
            "status": self.status.value,
            "error": self.error,
            "steps": [s.to_record() for s in self.steps],
        }
