"""The one test of "is this Docker container the SafeOps kind control plane?" (decision 0021).

Docker labels, nothing from Kubernetes: kind stamps every node container with the cluster
it belongs to and its role, and those labels stay inspectable while the API server is down.

Two callers, deliberately different in what they add on top:
- NodeInspector (decision 0012) also requires the node to publish the harness kubeconfig's
  API server, because node inspection must prove it is looking at the cluster the harness
  is talking to.
- `cluster-down` requires only this, because teardown must still work when the API server
  is broken: quarantine rebuilds run exactly then, and must not stall on it.

Standard library only; it takes the command runner as a parameter, for tests.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

CLUSTER_LABEL = "io.x-k8s.kind.cluster"
ROLE_LABEL = "io.x-k8s.kind.role"
CONTROL_PLANE_ROLE = "control-plane"
_FORMAT = (
    f'{{{{index .Config.Labels "{CLUSTER_LABEL}"}}}}|{{{{index .Config.Labels "{ROLE_LABEL}"}}}}'
)

Runner = Callable[..., subprocess.CompletedProcess]


class KindIdentityError(Exception):
    """The container isn't provably the expected kind control plane. Refuse."""


def control_plane_container(cluster_name: str) -> str:
    return f"{cluster_name}-control-plane"


def verify_control_plane(cluster_name: str, runner: Runner = subprocess.run) -> str:
    """Return the container name if its kind labels say it is `cluster_name`'s control
    plane; raise KindIdentityError otherwise, including when it can't be inspected."""
    container = control_plane_container(cluster_name)
    expected = f"cluster={cluster_name!r}, role={CONTROL_PLANE_ROLE!r}"
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
    if out.count("|") != 1:
        raise KindIdentityError(
            f"container {container!r}: unreadable kind labels {out!r}; expected {expected}"
        )
    cluster, role = out.split("|")
    if cluster != cluster_name or role != CONTROL_PLANE_ROLE:
        raise KindIdentityError(
            f"container {container!r} is not the SafeOps kind control plane: labels say"
            f" cluster={cluster!r}, role={role!r}; expected {expected}"
        )
    return container
