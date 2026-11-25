"""The executor's transport seam (decision 0025 D§11.3, D§11.6, D§11.7; M8B-1 amendment).

The executor core decides what an action is allowed to do and what a write's outcome means.
It never talks to Kubernetes itself. Two injected transports do:

- a **WriterTransport** (Milestone 8B-2: the safeops-executor identity) preflights every
  request before the first write and sends one request at a time;
- a **ReaderTransport** (Milestone 8B-2: the existing safeops-reader identity) reads objects
  for the two envelope pre-reads and for reconciliation.

What crosses the seam is semantic, never raw. A concrete adapter owns every reading of a
kubectl exit code, stderr, HTTP status, timeout or unparseable output, and translates it
into one closed `WriteClass` (or `ReadOutcome`). The core never parses kubectl text.

Neutral: stdlib, safeops.domain and safeops.enums only (the executor is in the
information-wall protected set).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from safeops.domain import MAX_DISCARD_ERROR
from safeops.enums import ActionType


def canonical_json(value: object) -> str:
    """Strict, canonical JSON text: sorted keys, no whitespace, no NaN or infinity.
    Raises (TypeError/ValueError) for anything that isn't JSON-safe."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json_copy(value: object) -> object:
    """A detached JSON-safe copy: nothing the caller keeps can change it afterwards."""
    return json.loads(canonical_json(value))


def _bounded_diagnostic(diagnostic: object) -> str | None:
    if diagnostic is None:
        return None
    if type(diagnostic) is not str:
        raise TypeError(f"a diagnostic is a str or None, not {type(diagnostic).__name__}")
    return diagnostic[:MAX_DISCARD_ERROR]


class WriteClass(StrEnum):
    """What one write's response means (D§11.3). Closed: an adapter maps every raw outcome
    to exactly one of these."""

    SUCCESS = "success"  # the adapter believes the request completed; the object may show it
    INVALID_ACTION = "invalid_action"  # Kubernetes' semantic 422 Invalid: nothing written
    DEFINITE_FAILURE = "definite_failure"  # 400/401/403/404/409/429...: treated as not applied
    NOT_ATTEMPTED = "not_attempted"  # the request was never sent (credential, audit-before)
    AMBIGUOUS = "ambiguous"  # may have applied: timeout, connection lost, 5xx, unparseable


@dataclass(frozen=True)
class WriteReply:
    semantic_class: WriteClass
    returned_object: object = None  # JSON-safe; the object the API server returned, if any
    diagnostic: str | None = None  # bounded (MAX_DISCARD_ERROR); never a token or Secret

    def __post_init__(self) -> None:
        if not isinstance(self.semantic_class, WriteClass):
            raise TypeError(f"semantic_class must be a WriteClass, not {self.semantic_class!r}")
        object.__setattr__(self, "returned_object", _json_copy(self.returned_object))
        object.__setattr__(self, "diagnostic", _bounded_diagnostic(self.diagnostic))


class ReadOutcome(StrEnum):
    FOUND = "found"  # a successful read, with the object (or list)
    ABSENT = "absent"  # a successful read proving the object does not exist
    UNAVAILABLE = "unavailable"  # the read could not be made or completed


@dataclass(frozen=True)
class ReadReply:
    outcome: ReadOutcome
    obj: object = None  # JSON-safe; present exactly when outcome is FOUND
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReadOutcome):
            raise TypeError(f"outcome must be a ReadOutcome, not {self.outcome!r}")
        if (self.outcome is ReadOutcome.FOUND) != (self.obj is not None):
            raise ValueError("a read reply carries an object exactly when it is FOUND")
        object.__setattr__(self, "obj", _json_copy(self.obj))
        object.__setattr__(self, "diagnostic", _bounded_diagnostic(self.diagnostic))


class PatchType(StrEnum):
    """kubectl patch --type values."""

    JSON = "json"  # RFC 6902
    MERGE = "merge"  # RFC 7386
    STRATEGIC = "strategic"  # strategic merge (containers merged by name)


class Check(StrEnum):
    """Which postcondition a write must establish (D§11.6)."""

    REPLICAS = "replicas"
    RESTART_ANNOTATION = "restart_annotation"
    TEMPLATE = "template"
    CONFIGMAP_DATA = "configmap_data"
    SELECTOR = "selector"
    RESOURCES = "resources"


@dataclass(frozen=True)
class Postcondition:
    check: Check
    expected_json: str  # canonical JSON of the expected value

    def expected(self):
        return json.loads(self.expected_json)


@dataclass(frozen=True)
class PreparedWrite:
    """One fully built write, immutable. It carries only what Kubernetes needs and what the
    response is checked against: no fault, harm, safe range, gate or diagnosis data."""

    operation: ActionType
    kind: str
    namespace: str
    name: str
    patch_type: PatchType
    body_json: str  # canonical JSON of the patch body
    postcondition: Postcondition

    def body(self):
        """A fresh parsed copy of the patch body."""
        return json.loads(self.body_json)


def prepared(
    operation: ActionType,
    kind: str,
    namespace: str,
    name: str,
    patch_type: PatchType,
    body: object,
    check: Check,
    expected: object,
) -> PreparedWrite:
    return PreparedWrite(
        operation=operation,
        kind=kind,
        namespace=namespace,
        name=name,
        patch_type=patch_type,
        body_json=canonical_json(body),
        postcondition=Postcondition(check, canonical_json(expected)),
    )


@runtime_checkable
class WriterTransport(Protocol):
    def preflight(self, requests: tuple[PreparedWrite, ...]) -> None:
        """Check every request the action will send, before the first is sent. Raises for a
        request the executor must never make (a harness bug, never a classification)."""

    def write(self, request: PreparedWrite) -> WriteReply:
        """Send one request (or not) and say what happened, semantically."""


@runtime_checkable
class ReaderTransport(Protocol):
    def get(self, kind: str, name: str, namespace: str, timeout_s: float) -> ReadReply:
        """One attempt to read one object."""

    def list(self, kind: str, namespace: str, timeout_s: float) -> ReadReply:
        """One attempt to list one kind in the namespace; FOUND carries {"items": [...]}."""
