"""Shared HTTP plumbing for both testbed images (safeops-testbed and safeops-loadgen).

This is testbed code that runs inside the cluster. It is not SafeOps runtime code, and
nothing under safeops/ imports it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE_NAME", "unknown")

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_LEVEL = _LEVELS.get(os.environ.get("LOG_LEVEL", "info").lower(), 20)

REQUESTS = Counter("http_requests_total", "HTTP requests served", ["service", "route", "code"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


def log(level: str, msg: str, **fields: object) -> None:
    """One JSON object per line on stdout, filtered by LOG_LEVEL."""
    if _LEVELS[level] < _LEVEL:
        return
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "service": SERVICE,
        "msg": msg,
        **fields,
    }
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def warm_cache() -> bytearray:
    """Hold CACHE_MB of resident memory, as a real in-process cache would.

    It gives every service a deterministic working set, so a memory limit set below it
    (memory-limit-low, Milestone 11) produces an OOM kill reliably. CACHE_MB lives in the
    pod template.
    """
    size = int(os.environ.get("CACHE_MB", "0")) * 1024 * 1024
    buf = bytearray(size)
    for i in range(0, size, 4096):  # touch every page so it is actually resident
        buf[i] = 1
    return buf


class Response(NamedTuple):
    status: int
    body: dict


Handler = Callable[[dict, dict], Response]


def is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


def call(url: str, method: str = "POST", payload: dict | None = None, timeout_s: float = 1.0):
    """HTTP call to another service. Returns (status, body); network errors propagate."""
    data = None if method == "GET" else json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}


class App:
    """Minimal router on the stdlib threading HTTP server."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.ready = threading.Event()

    def route(self, method: str, path: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self.routes[(method, path)] = fn
            return fn

        return register

    def serve(self, port: int = 8080) -> None:
        app = self

        class RequestHandler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # silence default access log
                pass

            def _send(self, status: int, payload: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _read_body(self, limit: int = 1 << 20) -> bytes:
                """Request body, with Content-Length or chunked transfer encoding.

                The API-server service proxy re-streams request bodies chunked, so the
                harness's PUT /control arrives without a Content-Length (decision 0009).
                """
                if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                    data = b""
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass  # discard trailers
                            return data
                        data += self.rfile.read(size)
                        self.rfile.readline()  # CRLF after each chunk
                        if len(data) > limit:
                            raise ValueError("body too large")
                length = int(self.headers.get("Content-Length") or 0)
                if length > limit:
                    raise ValueError("body too large")
                return self.rfile.read(length) if length else b""

            def _dispatch(self, method: str) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path == "/healthz":
                    return self._send(200, b'{"ok": true}')
                if path == "/readyz":
                    ok = app.ready.is_set()
                    return self._send(200 if ok else 503, json.dumps({"ready": ok}).encode())
                if path == "/metrics":
                    return self._send(200, generate_latest(), CONTENT_TYPE_LATEST)

                handler = app.routes.get((method, path))
                if handler is None:
                    return self._send(404, b'{"error": "not found"}')
                try:
                    raw = self._read_body()
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    return self._send(400, b'{"error": "invalid json"}')
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}

                start = time.monotonic()
                try:
                    resp = handler(body, query)
                except Exception as exc:  # a handler bug is a 500, never a crash
                    log("error", "unhandled error", route=path, error=type(exc).__name__)
                    resp = Response(500, {"error": "internal"})
                LATENCY.labels(SERVICE, path).observe(time.monotonic() - start)
                REQUESTS.labels(SERVICE, path, str(resp.status)).inc()
                self._send(resp.status, json.dumps(resp.body).encode())

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

        server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
        server.daemon_threads = True
        log("info", "listening", port=port)
        server.serve_forever()
