"""What every tool runs with: the safeops-reader client, the public topology, and the
current incident's evidence floor (decision 0023).

- The client is a `Kubectl` in Phase.READER: its own credential, never the harness's or the
  admin's; no mutation, enforced client-side by the guard and server-side by RBAC (§4).
- `not_before` is the start of the current run's load. Evidence older than it belongs to an
  earlier run (Pods, Events and Prometheus series outlive a reset), so every time-windowed
  tool starts there at the earliest, whatever window the model asks for (0017 §4).
- Every failure is a ToolError with its kind, never an empty result.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from safeops.cluster import CommandError, CredentialError, GuardError, Kubectl, Phase
from safeops.tools.contracts import ToolError
from safeops.topology import ServiceTopology

READ_TIMEOUT_S = 30


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ToolContext:
    kubectl: Kubectl
    topology: ServiceTopology
    not_before: datetime
    clock: Callable[[], datetime] = field(default=_now)
    timeout_s: float = READ_TIMEOUT_S

    def __post_init__(self) -> None:
        if getattr(self.kubectl, "phase", None) is not Phase.READER:
            raise GuardError("tools run only with the safeops-reader client (Phase.READER)")
        if not isinstance(self.topology, ServiceTopology):
            raise TypeError("ToolContext.topology must be a ServiceTopology")
        if self.not_before.tzinfo is None:
            raise ValueError("ToolContext.not_before must be timezone-aware")

    @classmethod
    def for_reader(cls, not_before: datetime) -> ToolContext:
        """The real context: safeops-reader's credential and config/topology.yaml."""
        from safeops.cluster import load_cluster_config
        from safeops.topology import load_topology

        return cls(Kubectl(load_cluster_config(), Phase.READER), load_topology(), not_before)

    @property
    def namespace(self) -> str:
        return self.kubectl.cfg.namespace

    def now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("the tool clock must be timezone-aware")
        return now.astimezone(UTC)

    def require_namespace(self, namespace: str) -> None:
        if namespace != self.namespace:
            raise ToolError(
                "invalid_argument",
                f"namespace {namespace!r} is outside the incident's scope; only"
                f" {self.namespace!r} can be read",
            )

    def require_service(self, service: str) -> None:
        if service not in self.topology.services:
            known = ", ".join(sorted(self.topology.services))
            raise ToolError("invalid_argument", f"unknown service {service!r} (known: {known})")

    def read(self, *args: str) -> str:
        """One read through the reader client; every failure becomes a typed ToolError."""
        return _guarded(lambda: self.kubectl.read(*args, timeout=self.timeout_s))

    def read_raw(self, *args: str) -> str:
        """read(), with the output taken as raw bytes and decoded explicitly (Kubectl.read_raw):
        for `kubectl logs`, so "\r" and every other non-"\n" character survive as data."""
        return _guarded(lambda: self.kubectl.read_raw(*args, timeout=self.timeout_s))

    def read_json(self, *args: str) -> dict:
        return _json(self.read(*args, "-o", "json"))

    def raw_get(self, path: str) -> dict:
        return _json(_guarded(lambda: self.kubectl.raw("GET", path, timeout=self.timeout_s)))


def _guarded(call: Callable[[], str]) -> str:
    try:
        return call()
    except CredentialError as exc:
        raise ToolError("credential", str(exc)) from exc
    except GuardError as exc:
        raise ToolError("guard", str(exc)) from exc
    except CommandError as exc:
        raise ToolError("command_failed", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError("timeout", f"the read did not finish in {exc.timeout}s") from exc
    except UnicodeDecodeError as exc:  # a text-mode read of undecodable output: never untyped
        raise ToolError("malformed_response", f"the output is not valid text: {exc}") from exc


def _json(text: str) -> dict:
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ToolError("malformed_response", f"the response is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ToolError("malformed_response", "the response is not a JSON object")
    return obj


def items(obj: dict) -> list[dict]:
    """The `items` of a list response, which must be a list of objects."""
    found = obj.get("items")
    if not isinstance(found, list) or not all(isinstance(i, dict) for i in found):
        raise ToolError("malformed_response", "a list response without a list of items")
    return found


def parse_time(value: object) -> datetime:
    """An RFC 3339 timestamp from the API server, as an aware UTC datetime. Nanosecond
    fractions (container logs) are truncated to microseconds."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"not a timestamp: {value!r}")
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        digits = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{head}.{rest[:digits][:6].ljust(6, '0')}{rest[digits:]}"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without a timezone: {value!r}")
    return dt.astimezone(UTC)


def cap(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)
