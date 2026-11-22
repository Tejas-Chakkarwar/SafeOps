"""The one test of "is this Docker container the SafeOps kind control plane?" (decisions
0021, 0022).

Docker only, nothing from Kubernetes. One `docker inspect` reads, in one observation, the
container's full immutable ID and the two labels kind stamps on every node container.
Those stay inspectable while the API server is down.

Two levels of identity:
- **Standard kind identity** (`verify_control_plane`): the labels say cluster
  `<cluster_name>`, role `control-plane`. That proves the name and the role only. A kind
  cluster recreated later under the same name, by anything, gets the same labels and the
  same container name.
- **Exact SafeOps instance identity** (`verify_owned`): additionally, the full container ID
  equals the one `cluster-up` recorded in `.safeops/cluster-identity.json` right after its
  own `kind create`. That pins the exact cluster this state directory created.

Callers:
- NodeInspector (decision 0012) uses the standard identity, then also requires the node to
  publish the harness kubeconfig's API server. Inspection asks "is this the node behind the
  API the harness is talking to?", so the API endpoint belongs there.
- `cluster-down` uses the exact identity and nothing from Kubernetes. Teardown asks "is
  this the exact kind cluster SafeOps created?", and must still work when Kubernetes is
  broken: quarantine rebuilds run exactly then.

There is no automatic adoption: an identity is only ever recorded for a cluster that
`cluster-up` itself just created. The only bypass of the ID match is the explicit
`--force-unverified-ownership` argument to `python -m safeops.bootstrap down`, given by an
operator for that one invocation. No environment variable or configuration enables it.

Standard library only; the command runner is a parameter, for tests.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

CLUSTER_LABEL = "io.x-k8s.kind.cluster"
ROLE_LABEL = "io.x-k8s.kind.role"
CONTROL_PLANE_ROLE = "control-plane"
_FORMAT = (
    "{{.Id}}"
    f'|{{{{index .Config.Labels "{CLUSTER_LABEL}"}}}}|{{{{index .Config.Labels "{ROLE_LABEL}"}}}}'
)
_FULL_ID = re.compile(r"[0-9a-f]{64}")

IDENTITY_FILE = "cluster-identity.json"
IDENTITY_SCHEMA_VERSION = 1
FORCE_FLAG = "--force-unverified-ownership"

Runner = Callable[..., subprocess.CompletedProcess]


class KindIdentityError(Exception):
    """The container isn't provably the expected kind control plane. Refuse."""


class OwnershipError(KindIdentityError):
    """The kind labels match, but this isn't provably the exact cluster SafeOps created."""


def control_plane_container(cluster_name: str) -> str:
    return f"{cluster_name}-control-plane"


def identity_path(state_dir: Path) -> Path:
    return state_dir / IDENTITY_FILE


def _expected(cluster_name: str) -> str:
    return f"cluster={cluster_name!r}, role={CONTROL_PLANE_ROLE!r}"


def _inspect(cluster_name: str, runner: Runner | None) -> tuple[str, str]:
    """(container, full container ID), once the labels say it's the control plane of
    `cluster_name`. Raises KindIdentityError otherwise, including when it can't be read."""
    container = control_plane_container(cluster_name)
    expected = _expected(cluster_name)
    runner = runner or subprocess.run  # resolved per call, never bound at import
    try:
        proc = runner(
            ["docker", "inspect", container, "--format", _FORMAT],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KindIdentityError(
            f"container {container!r} could not be inspected ({type(exc).__name__}: {exc});"
            f" expected {expected}"
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise KindIdentityError(
            f"container {container!r} could not be inspected"
            f" ({err[-1] if err else f'exit {proc.returncode}'}); expected {expected}"
        )
    out = (proc.stdout or "").strip()
    if out.count("|") != 2:
        raise KindIdentityError(
            f"container {container!r}: unreadable kind labels {out!r}; expected {expected}"
        )
    container_id, cluster, role = out.split("|")
    if cluster != cluster_name or role != CONTROL_PLANE_ROLE:
        raise KindIdentityError(
            f"container {container!r} is not the SafeOps kind control plane: labels say"
            f" cluster={cluster!r}, role={role!r}; expected {expected}"
        )
    if not _FULL_ID.fullmatch(container_id):
        raise KindIdentityError(
            f"container {container!r}: unreadable container ID {container_id!r};"
            f" expected {expected} with a full 64-hex ID"
        )
    return container, container_id


def verify_control_plane(cluster_name: str, runner: Runner | None = None) -> str:
    """Standard kind identity: return the container name if its labels say it is
    `cluster_name`'s control plane; raise KindIdentityError otherwise."""
    return _inspect(cluster_name, runner)[0]


# --- exact SafeOps instance identity -------------------------------------------------------------


def _durable_write(path: Path, text: str) -> None:
    """Write `text` to `path` atomically and durably: a private temporary file in the same
    directory, fsync (F_FULLFSYNC where it exists), rename over, then sync the directory.
    A crash leaves the old file or the new one, never a partial one."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
        full = getattr(fcntl, "F_FULLFSYNC", None)
        if full is not None:
            try:
                fcntl.fcntl(fd, full)
            except OSError:
                pass  # not supported on this file system; the fsync above still happened
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def record_identity(state_dir: Path, cluster_name: str, runner: Runner | None = None) -> str:
    """Pin the cluster `cluster-up` just created. Called only right after its own
    successful `kind create` (never for a cluster found already running: no adoption).
    Returns the full container ID."""
    container, container_id = _inspect(cluster_name, runner)
    record = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "cluster_name": cluster_name,
        "control_plane_container": container,
        "container_id": container_id,
    }
    _durable_write(identity_path(state_dir), json.dumps(record, indent=2) + "\n")
    return container_id


def _migration_guidance(cluster_name: str) -> str:
    return (
        " If this is the pre-pin SafeOps cluster created by an earlier SafeOps version,"
        " first verify independently that it is safe to delete, then do the one-time"
        f" migration: `uv run python -m safeops.bootstrap down {FORCE_FLAG}`, then"
        " `make cluster-up` (decision 0022). The override skips only the container-ID match;"
        " the kind labels are still required. The direct manual alternative,"
        f" `kind delete cluster --name {cluster_name}`, bypasses SafeOps ownership"
        " verification entirely."
    )


def _read_identity(path: Path, cluster_name: str) -> str:
    """The persisted full container ID. Raises OwnershipError if the file is missing,
    malformed, incomplete or for another cluster: never a fallback to the labels."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise OwnershipError(
            f"no exact ownership record at {path}: this may be a legacy pre-pin SafeOps"
            " cluster, or one SafeOps did not create."
        ) from None
    except OSError as exc:
        raise OwnershipError(f"ownership record {path} is unreadable: {exc}.") from exc
    try:
        record = json.loads(text)
    except ValueError as exc:
        raise OwnershipError(f"ownership record {path} is malformed: {exc}.") from exc
    expected = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "cluster_name": cluster_name,
        "control_plane_container": control_plane_container(cluster_name),
    }
    if not isinstance(record, dict) or set(record) != {*expected, "container_id"}:
        raise OwnershipError(f"ownership record {path} is incomplete or has unknown fields.")
    for key, value in expected.items():
        if record[key] != value or type(record[key]) is not type(value):
            raise OwnershipError(
                f"ownership record {path} is stale: {key} is {record[key]!r}, expected {value!r}."
            )
    container_id = record["container_id"]
    if not isinstance(container_id, str) or not _FULL_ID.fullmatch(container_id):
        raise OwnershipError(
            f"ownership record {path} has no full container ID ({container_id!r})."
        )
    return container_id


def verify_owned(
    state_dir: Path,
    cluster_name: str,
    runner: Runner | None = None,
    *,
    force_unverified_ownership: bool = False,
) -> str:
    """Exact SafeOps instance identity, for destructive teardown: the kind labels match and
    the full container ID equals the recorded one. Returns the container name; raises
    KindIdentityError (labels) or OwnershipError (exact instance) with operator guidance.

    `force_unverified_ownership` skips only the ID match. Only bootstrap's argv parser
    passes it, for the explicit operator flag."""
    path = identity_path(state_dir)
    try:
        container, current = _inspect(cluster_name, runner)
    except KindIdentityError as exc:
        raise KindIdentityError(f"{exc} (ownership record: {path})") from exc
    if force_unverified_ownership:
        return container
    try:
        persisted = _read_identity(path, cluster_name)
    except OwnershipError as exc:
        raise OwnershipError(
            f"{exc} Container {container!r} has the expected kind labels"
            f" ({_expected(cluster_name)}) and ID {current}, but exact ownership can't be"
            f" proven.{_migration_guidance(cluster_name)}"
        ) from exc
    if persisted != current:
        raise OwnershipError(
            f"container {container!r} is not the cluster SafeOps created: its ID is {current},"
            f" but the ownership record {path} says {persisted}. Its kind labels match"
            f" ({_expected(cluster_name)}), so a same-name cluster has replaced it."
            f"{_migration_guidance(cluster_name)}"
        )
    return container
