"""Workload generator: measurement infrastructure, not part of the system under test
(decision 0009). Ships as its own image (safeops-loadgen).

Two endpoints, reached by the harness through the API-server service proxy:
  PUT /control  {"action": "start", "rate_rps", "deadline_s", "path"}
                {"action": "measure", "attempts"}  -> {"window_id"}
                {"action": "stop"}
  GET /stats    ?window=<id> -> that window's summary; no id -> generator state

Load is open-loop: attempts are dispatched on schedule, never paced by responses.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from prometheus_client import Counter, Histogram

from app.common import App, Response, call, log
from app.window import Attempt, Window

GATEWAY_BASE_URL = os.environ["GATEWAY_BASE_URL"]

ATTEMPTS = Counter("loadgen_attempts_total", "Workload attempts by outcome", ["outcome"])
ATTEMPT_LATENCY = Histogram(
    "loadgen_attempt_duration_seconds",
    "Workload attempt latency (responses only)",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

app = App()
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="attempt")
_window_ids = itertools.count(1)
_state: dict = {
    "running": False,
    "rate_rps": None,
    "deadline_s": None,
    "path": None,
    "dispatched_total": 0,
    "stop": None,
    "generation": 0,  # bumped on every start; a stale scheduler thread can't touch a newer run
}
_windows: dict[str, Window] = {}
_collecting: Window | None = None


def _perform(attempt: Attempt, url: str, deadline_s: float) -> None:
    try:
        status, _ = call(url, "GET", None, deadline_s)
    except Exception:
        status = None
    done = time.monotonic()
    with _lock:
        attempt.status = status
        attempt.completed = done if status is not None else None
    if status is None:
        ATTEMPTS.labels("no_response").inc()
    else:
        ATTEMPT_LATENCY.observe(done - attempt.dispatched)
        ok = status == 200 and done - attempt.dispatched <= deadline_s
        ATTEMPTS.labels("success" if ok else "failure").inc()


def _schedule(
    rate_rps: float, deadline_s: float, url: str, stop: threading.Event, generation: int
) -> None:
    global _collecting
    start = time.monotonic()
    for seq in itertools.count():
        due = start + seq / rate_rps
        delay = due - time.monotonic()
        if delay > 0 and stop.wait(delay):
            return
        attempt = Attempt(seq=seq, due=due, dispatched=time.monotonic())
        with _lock:
            # Checked under the lock: a thread that passed a stop check just before a
            # stop+start must not admit into the next run's window (review M2).
            if stop.is_set() or _state["generation"] != generation:
                return
            _state["dispatched_total"] += 1
            if _collecting is not None:
                _collecting.admit(attempt)
                if _collecting.full:
                    _collecting = None
        _pool.submit(_perform, attempt, url, deadline_s)


@app.route("PUT", "/control")
def control(body: dict, query: dict) -> Response:
    global _collecting
    action = body.get("action")
    with _lock:
        if action == "start":
            if _state["running"]:
                return Response(409, {"error": "already running"})
            rate, deadline, path = body.get("rate_rps"), body.get("deadline_s"), body.get("path")
            if not (
                isinstance(rate, int | float)
                and rate > 0
                and isinstance(deadline, int | float)
                and deadline > 0
                and isinstance(path, str)
                and path.startswith("/")
            ):
                return Response(400, {"error": "start needs rate_rps > 0, deadline_s > 0, path"})
            if _collecting is not None:
                # A window can't outlive the run it was measuring (decision 0009 amendment).
                return Response(409, {"error": "a window is still collecting"})
            # A successful start is the run boundary: the previous run's windows and
            # counters go, so every run measures against an identically light process
            # (Milestone 4 fix list, A1). Stop does not clear, so a finished run stays
            # inspectable until the next start. Window ids keep increasing.
            _windows.clear()
            _state["dispatched_total"] = 0
            stop = threading.Event()
            _state["generation"] += 1
            _state.update(running=True, rate_rps=rate, deadline_s=deadline, path=path, stop=stop)
            threading.Thread(
                target=_schedule,
                args=(rate, deadline, GATEWAY_BASE_URL + path, stop, _state["generation"]),
                daemon=True,
            ).start()
            log("info", "load started", rate_rps=rate, deadline_s=deadline, path=path)
            return Response(200, {"running": True})
        if action == "measure":
            size = body.get("attempts")
            if not _state["running"]:
                return Response(409, {"error": "not running"})
            if _collecting is not None:
                return Response(409, {"error": "a window is already collecting"})
            if not (isinstance(size, int) and size > 0):
                return Response(400, {"error": "measure needs attempts > 0"})
            window = Window(f"w{next(_window_ids)}", size, _state["deadline_s"])
            _windows[window.window_id] = window
            _collecting = window
            return Response(200, {"window_id": window.window_id})
        if action == "stop":
            if _state["stop"] is not None:
                _state["stop"].set()
            if _collecting is not None:
                _collecting.fail("generator stopped before the window was filled")
                _collecting = None
            _state.update(running=False, stop=None)
            log("info", "load stopped")
            return Response(200, {"running": False})
    return Response(400, {"error": "action must be start, measure, or stop"})


@app.route("GET", "/stats")
def stats(body: dict, query: dict) -> Response:
    now = time.monotonic()
    with _lock:
        window_id = query.get("window")
        if window_id is None:
            public = {k: v for k, v in _state.items() if k != "stop"}
            return Response(200, {**public, "windows": sorted(_windows)})
        window = _windows.get(window_id)
        if window is None:
            return Response(404, {"error": "unknown window"})
        return Response(200, window.summary(now))


def _freeze_once(windows, now: float) -> None:
    """Freeze every window that has closed. Already-frozen windows are skipped: their
    summary never changes, and re-reading one would copy it on every tick (fix list, A2)."""
    for window in windows:
        if not window.frozen:
            window.summary(now)  # computes and freezes once complete; no-op otherwise


def _freeze_closed_windows() -> None:
    """Freeze each window the moment it closes, not when someone happens to read it (M1)."""
    while True:
        time.sleep(0.1)
        now = time.monotonic()
        with _lock:
            _freeze_once(_windows.values(), now)


threading.Thread(target=_freeze_closed_windows, daemon=True, name="window-freezer").start()
app.ready.set()

if __name__ == "__main__":
    app.serve()
