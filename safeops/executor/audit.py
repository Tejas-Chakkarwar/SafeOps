"""The executor's audit trail (design §11.6; decision 0025 D§11.8, M8B-2 amendment).

One append-only JSON Lines file per run, `<audit root>/<run_id>.jsonl`, outside the run
record (no record field, no schema version 3). The default root is `data/executor_audit/`
(gitignored, and refused by the repository policy); tests inject a temporary one.

Events, each one canonical JSON object per line with `event`, `run_id` and `at`:

- `before_write`: operation, target, patch type, the rendered patch, the identity. It is made
  durable (file and, for a new file, its directory entry fsynced) BEFORE the write is sent.
  If it can't be, the write is not attempted (`NOT_ATTEMPTED`): the only audit event that
  gates anything.
- `after_write`: the response class and its bounded diagnostic.
- `read`: every executor read attempt: purpose (`rollback_history`, `container_check`,
  `reconciliation`), attempt number, outcome, and for reconciliation only the compared
  fields (never a whole object).
- `final`: the ExecutionResult's status, error and steps; exactly one per returned result.

Nothing here ever holds a token, an Authorization header or a Secret value: the patches are
built from action params (approved ConfigMap keys, selectors, quantities, a template the
reader returned), and diagnostics come from kubectl, which prints none.

**Binding.** The sink is shared by the writer and the executor, and bound to the current run
with `bind_run(run_id)` before the action starts. Every append begins by checking that binding
(`_bound_run_id`, the one place HarnessMisuse is raised), before any path is built or any
filesystem call is made. There is no unbind; `bind_run` rebinds only between runs. Nothing
caches the run id: every append reads the current binding.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from safeops.cluster import REPO_ROOT
from safeops.domain import MAX_DISCARD_ERROR
from safeops.executor.transport import canonical_json

EXECUTOR_AUDIT_DIR = REPO_ROOT / "data" / "executor_audit"


class HarnessMisuse(Exception):
    """The executor was used outside its lifecycle: its audit sink has no current run binding.

    A harness bug, never an outcome. Its one semantic raise source is the sink's bound-run
    guard (`JsonlExecutorAudit._bound_run_id`), which every append runs first, before any
    filesystem work, and which the executor runs at the start of `apply()`. So it is raised
    before anything is sent: the HARNESS_BUGS premise (safeops/executor/executor.py). A new
    raise site must re-prove that premise before it is added."""


class AuditPersistenceError(Exception):
    """An audit event could not be persisted (a filesystem or serialization failure) on a
    bound sink. Only `before_write` turns this into a classification (NOT_ATTEMPTED)."""


def keep_evidence(append: Callable[[], None]) -> None:
    """Attempt one audit append once execution evidence already exists (a write that may
    have been sent, a reply, a reconciliation read, a produced ExecutionResult). A failure of
    any non-interrupt kind is dropped: the evidence it would describe wins. Interrupts
    (KeyboardInterrupt, SystemExit) propagate; BaseException is never caught."""
    try:
        append()
    except Exception:
        return


@runtime_checkable
class ExecutorAudit(Protocol):
    """What the executor and its writer need from the shared sink."""

    def bind_run(self, run_id: str) -> None: ...

    def require_bound(self) -> None: ...

    def before_write(self, request, identity: str) -> None: ...

    def after_write(self, request, reply) -> None: ...

    def read(self, **fields) -> None: ...

    def final(self, result) -> None: ...


def canonical_run_id(run_id: object) -> str:
    """A run id is a canonical UUID string (lowercase, hyphenated), as the runner allocates
    it; anything else is refused, so no path can be built from it."""
    if type(run_id) is not str:
        raise ValueError(f"a run id is a canonical UUID string, not {type(run_id).__name__}")
    try:
        canonical = str(uuid.UUID(run_id))
    except ValueError as exc:
        raise ValueError(f"run id {run_id[:64]!r} is not a UUID") from exc
    if canonical != run_id:
        raise ValueError(f"run id {run_id[:64]!r} is not in canonical UUID form")
    return run_id


class OsFs:
    """The filesystem calls the sink makes, one place, injectable so tests can prove their
    order and durability without touching the real disk semantics."""

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def lexists(self, path: Path) -> bool:
        return os.path.lexists(path)

    def mkdir(self, path: Path) -> None:
        os.mkdir(path, 0o700)

    def open(self, path: Path, flags: int, mode: int) -> int:
        return os.open(path, flags, mode)

    def write(self, fd: int, data: bytes) -> int:
        return os.write(fd, data)

    def fsync(self, fd: int) -> None:
        os.fsync(fd)

    def close(self, fd: int) -> None:
        os.close(fd)

    def fsync_dir(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _utc_now() -> datetime:
    return datetime.now(UTC)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NEW_FILE = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | _NOFOLLOW
_EXISTING_FILE = os.O_WRONLY | os.O_APPEND | _NOFOLLOW


class JsonlExecutorAudit:
    """The production sink: one append-only JSON Lines file per run (module docstring)."""

    def __init__(
        self,
        audit_root: Path | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        fs: OsFs | None = None,
    ) -> None:
        # The default root is read here, at construction (tests redirect the module constant).
        self.root = Path(audit_root) if audit_root is not None else EXECUTOR_AUDIT_DIR
        self._clock = clock
        self._fs = fs if fs is not None else OsFs()
        self._run_id: str | None = None

    # --- binding -----------------------------------------------------------------------

    def bind_run(self, run_id: str) -> None:
        """Bind the sink to the current run. Validates the id; touches no filesystem state.
        Rebinding is for the next run only: a run binds once, before its action."""
        self._run_id = canonical_run_id(run_id)

    def require_bound(self) -> None:
        """Refuse use without a current run binding (HarnessMisuse), before anything else."""
        self._bound_run_id()

    def _bound_run_id(self) -> str:
        # The one HarnessMisuse raise site. Every append calls this first, before any path is
        # built or any filesystem call is made.
        run_id = self._run_id
        if run_id is None:
            raise HarnessMisuse(
                "the executor audit sink has no current run binding (bind_run before the"
                " action); nothing was written or sent"
            )
        return run_id

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{canonical_run_id(run_id)}.jsonl"

    # --- events ------------------------------------------------------------------------

    def before_write(self, request, identity: str) -> None:
        self._append(
            "before_write",
            {
                "operation": request.operation.value,
                "kind": request.kind,
                "namespace": request.namespace,
                "name": request.name,
                "patch_type": request.patch_type.value,
                "patch": request.body(),
                "identity": identity,
            },
        )

    def after_write(self, request, reply) -> None:
        self._append(
            "after_write",
            {
                "operation": request.operation.value,
                "kind": request.kind,
                "namespace": request.namespace,
                "name": request.name,
                "response_class": reply.semantic_class.value,
                "diagnostic": _bounded(reply.diagnostic),
            },
        )

    def read(self, **fields) -> None:
        if "diagnostic" in fields:
            fields["diagnostic"] = _bounded(fields["diagnostic"])
        self._append("read", fields)

    def final(self, result) -> None:
        self._append("final", result.to_record())

    # --- persistence -------------------------------------------------------------------

    def _append(self, event: str, fields: dict) -> None:
        run_id = self._bound_run_id()  # first: no path, no filesystem call before it
        try:
            at = self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            line = canonical_json({**fields, "event": event, "run_id": run_id, "at": at})
            directory = self._durable_directory()
            self._append_line(directory / f"{run_id}.jsonl", (line + "\n").encode())
        except (OSError, ValueError, TypeError) as exc:
            raise AuditPersistenceError(f"audit {event}: {type(exc).__name__}: {exc}") from exc

    def _durable_directory(self) -> Path:
        """The audit root, created if absent. Each newly created directory's entry is made
        durable (its parent fsynced), up to and including the first ancestor that already
        existed, and no further."""
        fs = self._fs
        missing: list[Path] = []
        path = self.root
        while not fs.is_dir(path):
            if fs.lexists(path):
                raise NotADirectoryError(f"{path} exists and is not a directory")
            if path.parent == path:
                raise FileNotFoundError(f"no existing ancestor for {self.root}")
            missing.append(path)
            path = path.parent
        for directory in reversed(missing):  # outermost first
            fs.mkdir(directory)
        for directory in missing:  # innermost first, ending at the first existing ancestor
            fs.fsync_dir(directory.parent)
        return self.root

    def _append_line(self, path: Path, data: bytes) -> None:
        fs = self._fs
        try:
            fd, new = fs.open(path, _NEW_FILE, 0o600), True
        except FileExistsError:
            fd, new = fs.open(path, _EXISTING_FILE, 0), False
        try:
            view = memoryview(data)
            while view:
                written = fs.write(fd, bytes(view))
                if written <= 0:
                    raise OSError("the audit write made no progress")
                view = view[written:]
            fs.fsync(fd)
        finally:
            fs.close(fd)
        if new:
            fs.fsync_dir(path.parent)  # the new file's directory entry, durable too


def _bounded(text: object) -> str | None:
    if text is None:
        return None
    return str(text)[:MAX_DISCARD_ERROR]
