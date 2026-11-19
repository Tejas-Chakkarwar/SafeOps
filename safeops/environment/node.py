"""Node-level clean-state checks via host-side `docker exec` (decision 0012; 0003 F3).

This is a host-side experiment-harness capability. It operates outside Kubernetes RBAC and
is not performed by the experiment-controller ServiceAccount. It runs only fixed, read-only
inspection scripts that take no caller input, and only against the container proven to be
the SafeOps kind node. If any part of the identity check or the inspection fails, the caller
gets an exception, and verify_clean_state counts that as "not clean" (fail closed).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

# Residue signatures (0012). Extend here, with a planted-residue test, for new fault types.
RESIDUE_QDISCS = ("netem", "tbf")
RESIDUE_PROCESSES = ("stress-ng", "memStress")

# One representative pid per distinct network namespace, then every qdisc in each. Fixed
# text: nothing from the caller is interpolated into it.
_QDISC_SCRIPT = r"""
for q in /proc/[0-9]*; do n=$(readlink "$q/ns/net" 2>/dev/null) && echo "$n ${q#/proc/}"; done \
  | sort -u -k1,1 | while read -r ns pid; do
      nsenter -t "$pid" -n tc qdisc show 2>/dev/null | sed "s|^|$ns |"
    done
"""
_PROCESS_SCRIPT = "ps -eo pid=,comm=,args="


class NodeCheckError(Exception):
    """The node couldn't be identified or inspected. Fail closed: treat the node as not clean."""


Runner = Callable[..., subprocess.CompletedProcess]


class NodeInspector:
    def __init__(
        self, cfg, runner: Runner = subprocess.run, expected_api_server: str | None = None
    ):
        """`expected_api_server` is the harness kubeconfig's API server URL; the node must be
        the container Docker publishes it from."""
        self.cfg = cfg
        self.node = f"{cfg.cluster_name}-control-plane"
        self._run = runner
        self._expected_api_server = expected_api_server
        self._verified = False

    def _docker(self, *args: str, timeout: float = 60) -> str:
        try:
            proc = self._run(
                ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NodeCheckError(f"docker {args[0]} failed: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            raise NodeCheckError(f"docker {args[0]} failed: {err[-1] if err else proc.returncode}")
        return proc.stdout

    def verify_identity(self) -> None:
        """Node name, kind labels, and the published API port must all match (0012)."""
        if self._verified:
            return
        fmt = (
            '{{index .Config.Labels "io.x-k8s.kind.cluster"}}|'
            '{{index .Config.Labels "io.x-k8s.kind.role"}}'
        )
        cluster, _, role = (
            self._docker("inspect", self.node, "--format", fmt).strip().partition("|")
        )
        if cluster != self.cfg.cluster_name or role != "control-plane":
            raise NodeCheckError(
                f"container {self.node!r} is not the SafeOps kind control-plane"
                f" (cluster={cluster!r}, role={role!r})"
            )
        if self._expected_api_server is None:
            raise NodeCheckError("no expected API server to match the node against")
        published = {
            f"https://{line.strip()}"
            for line in self._docker("port", self.node, "6443/tcp").splitlines()
            if line.strip().startswith("127.0.0.1:")
        }
        if self._expected_api_server not in published:
            raise NodeCheckError(
                f"node {self.node!r} doesn't publish the harness API server"
                f" {self._expected_api_server}"
            )
        self._verified = True

    def qdisc_residue(self) -> list[str]:
        self.verify_identity()
        out = self._docker("exec", self.node, "sh", "-c", _QDISC_SCRIPT, timeout=120)
        found = []
        for line in out.splitlines():
            parts = line.split()
            # "<netns> qdisc <kind> <handle> ..." from the script's prefixing
            if len(parts) >= 3 and parts[1] == "qdisc" and parts[2] in RESIDUE_QDISCS:
                found.append(
                    f"{parts[2]} qdisc in network namespace {parts[0]}: {' '.join(parts[1:])}"
                )
        return found

    def process_residue(self) -> list[str]:
        self.verify_identity()
        out = self._docker("exec", self.node, "sh", "-c", _PROCESS_SCRIPT)
        found = []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            pid, comm = parts[0], parts[1]
            args = parts[2] if len(parts) > 2 else ""
            argv0 = args.split()[0].rsplit("/", 1)[-1] if args.split() else ""
            if comm in RESIDUE_PROCESSES or argv0 in RESIDUE_PROCESSES:
                found.append(f"{comm} process on the node (pid {pid}): {args[:120]}")
        return found

    def residue(self) -> list[str]:
        return self.qdisc_residue() + self.process_residue()
