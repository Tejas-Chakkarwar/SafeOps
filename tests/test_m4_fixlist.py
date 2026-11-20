"""Milestone 4 post-review fix list (decision 0015, third amendment; decision 0009 amendment).

Offline. The load generator (testbed/app/loadgen.py) is loaded with stand-ins for
prometheus_client and app.common, which only exist inside its image; app.window is real.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "testbed" / "app"


# --- Group A: the load generator's run boundary -------------------------------------------------


class _Response(NamedTuple):
    status: int
    body: dict


def _load_loadgen(monkeypatch):
    """A fresh loadgen module (its state is module-global), with calls answered 200 at once."""

    class _Metric:
        def __init__(self, *a, **k):
            pass

        def labels(self, *a):
            return self

        def inc(self, *a):
            pass

        def observe(self, *a):
            pass

    prom = types.ModuleType("prometheus_client")
    prom.Counter = prom.Histogram = _Metric

    class App:
        def __init__(self):
            self.ready = threading.Event()

        def route(self, method, path):
            return lambda fn: fn

    common = types.ModuleType("app.common")
    common.App, common.Response, common.log = App, _Response, lambda *a, **k: None
    common.call = lambda url, method, payload, timeout: (200, {})
    pkg = types.ModuleType("app")
    pkg.__path__ = [str(APP_DIR)]
    for name, mod in {"prometheus_client": prom, "app": pkg, "app.common": common}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "app.window", raising=False)
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://gateway-svc:8080")
    spec = importlib.util.spec_from_file_location("app.loadgen", APP_DIR / "loadgen.py")
    loadgen = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "app.loadgen", loadgen)
    spec.loader.exec_module(loadgen)
    return loadgen


START = {"action": "start", "rate_rps": 500, "deadline_s": 1.0, "path": "/checkout"}


def _control(lg, body) -> _Response:
    return lg.control(body, {})


def _stats(lg, window_id=None) -> _Response:
    return lg.stats({}, {} if window_id is None else {"window": window_id})


def _wait_full(lg, window_id, timeout=5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _stats(lg, window_id).body.get("scheduled") == _stats(lg, window_id).body.get("size"):
            return
        time.sleep(0.01)
    raise AssertionError(f"window {window_id} never filled")


def test_start_is_the_run_boundary(monkeypatch) -> None:
    lg = _load_loadgen(monkeypatch)
    assert _control(lg, START).status == 200
    w1 = _control(lg, {"action": "measure", "attempts": 5}).body["window_id"]
    _wait_full(lg, w1)
    assert _control(lg, {"action": "stop"}).status == 200
    # Stop does not clear: the finished run stays inspectable until the next start.
    assert _stats(lg).body["windows"] == [w1]
    assert _stats(lg, w1).status == 200
    assert _stats(lg).body["dispatched_total"] >= 5

    assert _control(lg, START).status == 200
    assert _stats(lg).body["windows"] == []
    assert _stats(lg, w1).status == 404
    w2 = _control(lg, {"action": "measure", "attempts": 5}).body["window_id"]
    assert w2 != w1  # ids keep increasing
    _wait_full(lg, w2)
    assert _stats(lg).body["windows"] == [w2]
    _control(lg, {"action": "stop"})


def test_dispatched_total_restarts_at_zero(monkeypatch) -> None:
    lg = _load_loadgen(monkeypatch)
    _control(lg, START)
    time.sleep(0.1)
    _control(lg, {"action": "stop"})
    assert lg._state["dispatched_total"] > 0
    lg._state["dispatched_total"] = 10_000  # a long history from earlier runs
    _control(lg, START)
    # At 500 req/s only a handful can have been dispatched since the reset.
    assert lg._state["dispatched_total"] < 100
    _control(lg, {"action": "stop"})


def test_fifty_cycles_keep_only_the_current_run(monkeypatch) -> None:
    lg = _load_loadgen(monkeypatch)
    for _ in range(50):
        assert _control(lg, START).status == 200
        assert len(lg._windows) == 0
        wid = _control(lg, {"action": "measure", "attempts": 3}).body["window_id"]
        _wait_full(lg, wid)
        assert len(lg._windows) == 1
        _control(lg, {"action": "stop"})
    assert len(lg._windows) == 1
    assert int(wid.lstrip("w")) == 50


def test_start_is_refused_while_a_window_is_collecting(monkeypatch) -> None:
    lg = _load_loadgen(monkeypatch)
    # Through the API: still running, so refused before anything is cleared.
    _control(lg, START)
    _control(lg, {"action": "measure", "attempts": 10**6})
    assert _control(lg, START).status == 409
    _control(lg, {"action": "stop"})
    # Defensively: a collecting window left behind with no run active.
    window = lg.Window("w99", 10, 1.0)
    lg._windows["w99"] = window
    lg._collecting = window
    response = _control(lg, START)
    assert response.status == 409 and "collecting" in response.body["error"]
    assert "w99" in lg._windows  # nothing was cleared by the refused start
    assert lg._state["running"] is False


def _closed_window(lg, wid="w1"):
    w = lg.Window(wid, 2, 1.0)
    for i in range(2):
        a = lg.Attempt(seq=i, due=0.0, dispatched=0.0)
        a.status, a.completed = 200, 0.01
        w.admit(a)
    return w


def test_freezer_freezes_a_window_the_moment_it_closes(monkeypatch) -> None:
    lg = _load_loadgen(monkeypatch)
    w = _closed_window(lg)
    close = 0.0 + 1.0 + w.settle_s
    lg._freeze_once([w], close - 0.01)
    assert not w.frozen  # still draining
    lg._freeze_once([w], close)
    assert w.frozen and w.summary(close + 100)["state"] == "complete"


def test_freezer_skips_windows_that_are_already_frozen(monkeypatch) -> None:
    lg = _load_loadgen(monkeypatch)
    w = _closed_window(lg)
    lg._freeze_once([w], 10.0)
    assert w.frozen
    calls = []
    real = w.summary
    w.summary = lambda now: calls.append(now) or real(now)  # type: ignore[method-assign]
    for tick in range(100):
        lg._freeze_once([w], 10.0 + tick)
    assert calls == []
    # Callers still get a defensive copy.
    first = w.summary(20.0)
    first["succeeded"] = -1
    assert w.summary(21.0)["succeeded"] == 2


# --- Group B ------------------------------------------------------------------------------------

from safeops.environment.workload import (  # noqa: E402
    WINDOW_CLOSE_MARGIN_S,
    WorkloadGenerator,
    WorkloadSettings,
    _inconsistency,
)
from safeops.experiment.unit import RunUnit  # noqa: E402
from safeops.faults import injector as injector_mod  # noqa: E402
from safeops.faults.injector import (  # noqa: E402
    VERIFY_MAX_WINDOWS,
    VERIFY_NO_NEW_WINDOW_AFTER_S,
    FaultBackendRef,
    InjectedFault,
)
from safeops.faults.spec import FaultSpecError, load_fault_spec, parse_fault_spec  # noqa: E402
from safeops.topology import load_topology  # noqa: E402
from tests.test_m4_faults import (  # noqa: E402
    SETTINGS,
    FakeDetectionWorkload,
    FakeKubectl,
    _fault,
    _injector,
    _LoadRecorder,
    _orders_deployment,
    _spec_text,
)

SPEC = load_fault_spec("bad-deployment-config")


def test_injected_fault_backend_comes_from_the_selected_spec_and_is_frozen() -> None:
    """Item 3: provenance is derived, never a hardcoded claim; and it can't be mutated."""
    fault = _injector(FakeKubectl(_orders_deployment())).inject("bad-deployment-config", 0)
    assert fault.backend == FaultBackendRef(name=SPEC.injection.backend, version=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        fault.backend.name = "chaos-mesh"  # type: ignore[misc]
    assert {f.name for f in dataclasses.fields(InjectedFault)} >= {"backend"}
    field = next(f for f in dataclasses.fields(InjectedFault) if f.name == "backend")
    assert field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING


def test_backend_ref_follows_a_different_backend_name(monkeypatch) -> None:
    """If the selected backend reports another name, the record says so (no silent claim)."""

    class OtherBackend(injector_mod.SetContainerEnv):
        name = "another-backend"
        version = "9.9"

    monkeypatch.setattr(injector_mod, "SetContainerEnv", OtherBackend)
    with pytest.raises(FaultSpecError, match="another-backend"):
        _injector(FakeKubectl(_orders_deployment())).inject("bad-deployment-config", 0)


def test_lost_start_response_still_stops_the_load(offline_runner_factory) -> None:
    """Item 4: the generator may have started even though the control call failed."""
    from safeops.cluster import CommandError

    r = offline_runner_factory()
    r.workload = _LoadRecorder(start_raises=CommandError("kubectl replace failed: timed out"))
    with pytest.raises(CommandError):
        r.run(RunUnit("bad-deployment-config", 0, 0))
    assert r.workload.events == ["start", "stop"]


def test_busy_generator_is_still_never_stopped(offline_runner_factory) -> None:
    """Item 4 keeps decision 0009 W5: a 409 means someone else's load. Never clobber it."""
    from safeops.environment.workload import WorkloadGeneratorBusy

    r = offline_runner_factory()
    r.workload = _LoadRecorder(start_raises=WorkloadGeneratorBusy("busy"))
    with pytest.raises(WorkloadGeneratorBusy):
        r.run(RunUnit("bad-deployment-config", 0, 0))
    assert r.workload.events == ["start"]


def test_settings_refuse_a_steady_state_window_that_cannot_close_in_time() -> None:
    """Item 5: 600 attempts at 10 req/s close ~62.5 s in, past the 60 s poll timeout."""
    assert SETTINGS.closes_after_s(300) == 30 + 1.0 + WINDOW_CLOSE_MARGIN_S
    with pytest.raises(ValueError, match="poll timeout"):
        dataclasses.replace(SETTINGS, window_attempts=600, min_successes=594)
    from safeops.environment.workload import load_workload_settings

    load_workload_settings()  # the committed configuration is possible


def test_detection_windows_use_the_same_invariant() -> None:
    spec = dataclasses.replace(
        SPEC,
        symptoms=(
            dataclasses.replace(SPEC.symptoms[0], expression="http_5xx_rate > 0.2 over 58s"),
        ),
    )
    with pytest.raises(FaultSpecError, match="poll timeout"):
        _injector(FakeKubectl(_orders_deployment()), FakeDetectionWorkload([], [0.0])).inject_spec(
            spec, 0
        )


def test_inconsistency_parameter_is_not_shadowed() -> None:
    """Item 6: the expected size is a named parameter, the observed size a local."""
    import inspect

    assert "expected_size" in inspect.signature(_inconsistency).parameters
    s = dict(state="complete", size=5, scheduled=5, succeeded=5, failed=0, deadline_s=1.0)
    assert _inconsistency(s, SETTINGS, 5) is None
    assert "configured 6" in _inconsistency(s, SETTINGS, 6)


def test_a_metric_without_alert_names_is_a_spec_error(monkeypatch) -> None:
    """Item 7: never a raw KeyError."""
    from safeops.faults import spec as spec_mod

    monkeypatch.setattr(spec_mod, "SUPPORTED_METRICS", spec_mod.SUPPORTED_METRICS | {"failed_rate"})
    text = _spec_text(
        symptoms=[
            {
                "alert_name": "HighErrorRate",
                "target_service": "orders",
                "expression": "failed_rate > 0.2 over 30s",
            }
        ]
    )
    with pytest.raises(FaultSpecError, match="no alert names"):
        parse_fault_spec(text.encode(), "bad-deployment-config", load_topology())


def test_verification_threshold_is_between_windows_and_reports_real_elapsed() -> None:
    """Item 8: two 80 s windows return at 160 s; the reason must say 160, not only 150."""
    assert VERIFY_NO_NEW_WINDOW_AFTER_S == 150.0 and VERIFY_MAX_WINDOWS == 3
    clock = [0.0]
    wl = FakeDetectionWorkload([0, 0, 0], clock, window_takes_s=80.0)
    v = _injector(None, wl, clock=lambda: clock[0]).verify_fault_present(_fault())
    assert not v.present and len(v.observations) == 2
    assert "after 160s" in v.reason and "no new window after 150s" in v.reason
    assert not hasattr(injector_mod, "VERIFY_TIMEOUT_S")


def test_workload_generator_construction_is_unchanged() -> None:
    WorkloadGenerator()
    assert isinstance(SETTINGS, WorkloadSettings)


def test_backend_ref_carries_what_the_selected_backend_reports(monkeypatch) -> None:
    """A hardcoded {"kubernetes-api", None} would pass whenever the names happen to agree;
    the version proves the reference is read from the backend that ran."""

    class VersionedBackend(injector_mod.SetContainerEnv):
        version = "1.2.3"

    monkeypatch.setattr(injector_mod, "SetContainerEnv", VersionedBackend)
    fault = _injector(FakeKubectl(_orders_deployment())).inject("bad-deployment-config", 0)
    assert fault.backend == FaultBackendRef("kubernetes-api", "1.2.3")
