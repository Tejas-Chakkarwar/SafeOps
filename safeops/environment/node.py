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

# Every network namespace on the node: every distinct /proc netns, plus every namespace
# pinned under /var/run/netns, even one with no process left in it (review L3). Then every
# qdisc in each, deduplicated. Fixed text: nothing from the caller is interpolated into it.
#
# Fail closed (decision 0021). A namespace that still exists but can't be inspected prints
# QDISC_ERROR_MARKER, never nothing: tc missing, nsenter refused, or empty tc output (every
# namespace has at least a loopback qdisc). Discovery races stay quiet: a /proc entry that
# vanishes while being listed is skipped, and each namespace's processes are tried oldest
# first, so one transient process can't trip the check. A namespace whose processes are
# all gone has been destroyed (it can't hold residue) and prints QDISC_GONE_MARKER.
QDISC_ERROR_MARKER = "SAFEOPS_QDISC_INSPECTION_ERROR:"
QDISC_GONE_MARKER = "SAFEOPS_QDISC_NAMESPACE_GONE:"
_QDISC_SCRIPT = r"""
ERR='SAFEOPS_QDISC_INSPECTION_ERROR:'
GONE='SAFEOPS_QDISC_NAMESPACE_GONE:'
{
for q in /proc/[0-9]*; do n=$(readlink "$q/ns/net" 2>/dev/null) && echo "$n ${q#/proc/}"; done \
  | sort -k1,1 -k2,2n | awk '{p[$1] = p[$1] " " $2} END {for (n in p) print n p[n]}' \
  | while read -r ns pids; do
      inspected=""; alive=""; last=""
      for pid in $pids; do
        if out=$(nsenter -t "$pid" -n tc qdisc show 2>&1); then
          if [ -n "$out" ]; then printf '%s\n' "$out" | sed "s|^|$ns |"
          else echo "$ERR $ns pid $pid: tc printed no qdiscs"; fi
          inspected=1; break
        fi
        last=$(printf '%s' "$out" | head -1)
        [ -e "/proc/$pid" ] && alive=1
      done
      if [ -z "$inspected" ]; then
        if [ -n "$alive" ]; then echo "$ERR $ns: every process failed: $last"
        else echo "$GONE $ns"; fi
      fi
    done
for f in /var/run/netns/*; do
  [ -e "$f" ] || continue
  if ino=$(stat -L -c %i "$f" 2>&1) && out=$(nsenter --net="$f" tc qdisc show 2>&1); then
    if [ -n "$out" ]; then printf '%s\n' "$out" | sed "s|^|net:[$ino] |"
    else echo "$ERR $f: tc printed no qdiscs"; fi
  elif [ -e "$f" ]; then echo "$ERR $f: $(printf '%s' "$out$ino" | head -1)"
  else echo "$GONE $f"; fi
done
} | sort -u
"""
_PROCESS_SCRIPT = "ps -eo pid=,comm=,args="


class NodeCheckError(Exception):
    """The node couldn't be identified or inspected. Fail closed: treat the node as not clean."""


def parse_qdisc_output(out: str) -> list[str]:
    """Residue from the qdisc script's output. Raises NodeCheckError when the inspection
    itself failed: an error marker anywhere, or no inspected qdisc at all (decision 0021)."""
    errors = [ln for ln in out.splitlines() if ln.startswith(QDISC_ERROR_MARKER)]
    if errors:
        raise NodeCheckError(
            f"qdisc inspection failed for {len(errors)} network namespace(s): {errors[0]}"
        )
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith(QDISC_GONE_MARKER)]
    if not lines:
        raise NodeCheckError(
            "qdisc inspection returned no qdiscs at all; refusing to call it clean"
        )
    found = []
    for line in lines:
        parts = line.split()
        # "<netns> qdisc <kind> <handle> ..." from the script's prefixing
        if len(parts) >= 3 and parts[1] == "qdisc" and parts[2] in RESIDUE_QDISCS:
            found.append(f"{parts[2]} qdisc in network namespace {parts[0]}: {' '.join(parts[1:])}")
    return found


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
        """Node name, kind labels, and the published API port must all match (0012).

        The label check is the shared primitive teardown uses too (decision 0021). On top of
        it, inspection also requires the harness API server, which teardown deliberately
        does not: a node is inspected only if it's the cluster the harness talks to.
        """
        if self._verified:
            return
        from safeops.kind_identity import KindIdentityError, verify_control_plane

        try:
            verify_control_plane(self.cfg.cluster_name, self._run)
        except KindIdentityError as exc:
            raise NodeCheckError(str(exc)) from exc
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
        return parse_qdisc_output(out)

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
