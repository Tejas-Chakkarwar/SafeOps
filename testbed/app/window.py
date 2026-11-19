"""Fixed-denominator measurement windows (decision 0009).

Pure accounting with no I/O and no third-party imports, so SafeOps tests can exercise it
directly. This code decides the steady-state baseline that `effective` is measured
against, so it is hand-written and tested against fabricated attempts.

Rules:
- A window is exactly `size` scheduled attempts, never a time slice.
- An attempt succeeds only with HTTP 200 inside its deadline (measured from dispatch).
  A timeout, an error, a non-200 response, or no response at all is a failed attempt.
- The window closes once the last attempt's deadline, plus a short settle margin, has
  passed. Outcomes are classified from the recorded response time, not from when the
  record was written, so every attempt's outcome is fixed by then and the last requests
  are counted consistently. The settle margin only covers the gap between a response
  arriving and its outcome being recorded. It never extends the deadline.
- If the generator can't schedule all `size` attempts (it was stopped, for example),
  that is a measurement failure, never a smaller denominator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COLLECTING = "collecting"
DRAINING = "draining"
COMPLETE = "complete"
FAILED = "failed"


@dataclass
class Attempt:
    seq: int
    due: float  # scheduled dispatch time (monotonic seconds)
    dispatched: float  # actual dispatch time
    completed: float | None = None
    status: int | None = None  # HTTP status, or None if no response arrived

    def succeeded(self, deadline_s: float) -> bool:
        return (
            self.status == 200
            and self.completed is not None
            and self.completed - self.dispatched <= deadline_s
        )


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile. None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-int(q * 100) * len(ordered) // 100))  # ceil(q * n)
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class Window:
    window_id: str
    size: int
    deadline_s: float
    settle_s: float = 0.5
    attempts: list[Attempt] = field(default_factory=list)
    failure: str | None = None

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError("window size must be positive")
        if self.deadline_s <= 0:
            raise ValueError("deadline must be positive")
        if self.settle_s < 0:
            raise ValueError("settle margin must be non-negative")

    @property
    def full(self) -> bool:
        return len(self.attempts) >= self.size

    def admit(self, attempt: Attempt) -> bool:
        """Add a newly dispatched attempt if the window still needs one."""
        if self.full or self.failure is not None:
            return False
        self.attempts.append(attempt)
        return True

    def fail(self, reason: str) -> None:
        """Mark a measurement failure, e.g. the generator stopped before filling the window."""
        if not self.full and self.failure is None:
            self.failure = reason

    def state(self, now: float) -> str:
        if self.failure is not None:
            return FAILED
        if not self.full:
            return COLLECTING
        last_dispatch = max(a.dispatched for a in self.attempts)
        return COMPLETE if now >= last_dispatch + self.deadline_s + self.settle_s else DRAINING

    def summary(self, now: float) -> dict:
        state = self.state(now)
        out: dict = {
            "window_id": self.window_id,
            "state": state,
            "size": self.size,
            "scheduled": len(self.attempts),
            "deadline_s": self.deadline_s,
            "failure": self.failure,
        }
        if state != COMPLETE:
            return out
        ok = [a for a in self.attempts if a.succeeded(self.deadline_s)]
        no_response = [
            a
            for a in self.attempts
            if a.status is None
            or a.completed is None
            or a.completed - a.dispatched > self.deadline_s
        ]
        latencies = [a.completed - a.dispatched for a in ok if a.completed is not None]
        p95 = _percentile(latencies, 0.95)
        out.update(
            succeeded=len(ok),
            failed=self.size - len(ok),
            timed_out_or_no_response=len(no_response),
            error_responses=self.size - len(ok) - len(no_response),
            success_rate=len(ok) / self.size,
            p95_latency_ms=None if p95 is None else round(p95 * 1000, 2),
            max_dispatch_lag_ms=round(max(a.dispatched - a.due for a in self.attempts) * 1000, 2),
        )
        return out
