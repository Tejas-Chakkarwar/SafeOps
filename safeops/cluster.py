"""Cluster identity, the phase-aware context guard, and the only way SafeOps code runs
kubectl (decisions 0002, 0006, 0009).

- Every kubectl call passes --kubeconfig and --context from config/cluster.yaml.
  `kubectl config current-context` is never read.
- The phase picks the credential. Runtime always uses the experiment-controller token
  and never falls back to the admin kubeconfig. Bootstrap uses the admin kubeconfig
  that kind wrote, and only safeops.bootstrap may ask for it
  (tests/test_cluster_guard.py enforces that).
- Kubernetes-object mutations go through `check_object`. Runtime allows the testbed
  namespace only; bootstrap allows its namespaces plus an exactly recorded set of
  cluster-scoped objects (config/bootstrap_scope.yaml).
- The workload-generator proxy is not an object mutation. It has its own capability
  check: one namespace, one Service, operations {stats, control}.

No secret ever reaches stdout, stderr or an exception message from this module.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLUSTER_CONFIG_PATH = REPO_ROOT / "config" / "cluster.yaml"

# Read-only kubectl verbs that `Kubectl.read` accepts. Anything that mutates goes
# through a dedicated, guarded method instead.
_READ_VERBS = frozenset({"get", "wait", "auth", "api-resources", "version"})
_READ_SUBCOMMANDS = {"rollout": frozenset({"status"})}


class GuardError(Exception):
    """A command would act outside its phase's scope."""


class CredentialError(Exception):
    """The credential for a phase is missing, invalid, or expired."""


class CommandError(Exception):
    """A kubectl, helm or kind command failed."""


class Phase(Enum):
    RUNTIME = "runtime"
    BOOTSTRAP = "bootstrap"


@dataclass(frozen=True)
class ClusterConfig:
    cluster_name: str
    kube_context: str
    kind_node_image: str
    kubeconfig_path: Path
    harness_kubeconfig_path: Path
    harness_token_meta_path: Path
    harness_session_s: int
    namespace: str
    system_namespace: str
    workload_namespace: str
    workload_service: str
    workload_service_port: int
    monitoring_namespace: str
    chaos_namespace: str
    postgres_image: str
    chaos_mesh_repo: str
    chaos_mesh_version: str
    prometheus_chart_repo: str
    prometheus_chart_version: str
    prometheus_service: str
    prometheus_service_port: int

    @property
    def bootstrap_namespaces(self) -> tuple[str, ...]:
        return (
            self.namespace,
            self.system_namespace,
            self.workload_namespace,
            self.monitoring_namespace,
            self.chaos_namespace,
        )


_PATH_KEYS = ("kubeconfig_path", "harness_kubeconfig_path", "harness_token_meta_path")
_INT_KEYS = ("harness_session_s", "workload_service_port", "prometheus_service_port")


def parse_cluster_config(data: dict, root: Path = REPO_ROOT) -> ClusterConfig:
    fields = ClusterConfig.__dataclass_fields__
    missing = sorted(k for k in fields if k not in data)
    if missing:
        raise ValueError(f"config/cluster.yaml is missing: {', '.join(missing)}")
    values = {k: data[k] for k in fields}
    for key in _PATH_KEYS:
        values[key] = (root / str(values[key])).resolve()
    for key in _INT_KEYS:
        values[key] = int(values[key])
    for key in fields:
        if key not in _PATH_KEYS and key not in _INT_KEYS:
            values[key] = str(values[key])
    cfg = ClusterConfig(**values)
    if cfg.kube_context != f"kind-{cfg.cluster_name}":
        raise ValueError("kube_context must be 'kind-' + cluster_name (kind's naming)")
    if len(set(cfg.bootstrap_namespaces)) != len(cfg.bootstrap_namespaces):
        raise ValueError("SafeOps namespaces must be distinct")
    return cfg


def load_cluster_config(path: Path = CLUSTER_CONFIG_PATH) -> ClusterConfig:
    import yaml

    return parse_cluster_config(yaml.safe_load(path.read_text()), root=path.parent.parent)


def check_object(
    cfg: ClusterConfig,
    phase: Phase,
    *,
    kind: str,
    name: str,
    namespace: str | None,
    cluster_scope: frozenset[tuple[str, str]] = frozenset(),
) -> None:
    """Refuse a Kubernetes-object mutation outside the phase's scope (decision 0006).

    namespace=None means a cluster-scoped object.
    """
    if namespace is None:
        if phase is Phase.RUNTIME:
            raise GuardError(f"runtime may not mutate cluster-scoped {kind}/{name}")
        if (kind, name) not in cluster_scope:
            raise GuardError(f"cluster-scoped {kind}/{name} is not in the recorded bootstrap scope")
        return
    allowed = (cfg.namespace,) if phase is Phase.RUNTIME else cfg.bootstrap_namespaces
    if namespace not in allowed:
        raise GuardError(
            f"{phase.value} may not mutate {kind}/{name} in namespace {namespace!r}"
            f" (allowed: {', '.join(allowed)})"
        )


# --- credentials ---------------------------------------------------------------------


def decode_token_times(token: str) -> tuple[int, int]:
    """Return (iat, exp) from a JWT payload. Never logs or returns the token itself."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return int(claims["iat"]), int(claims["exp"])
    except Exception as exc:
        raise CredentialError("could not read the issued token's lifetime") from exc


def harness_credential(cfg: ClusterConfig, now: Callable[[], float] = time.time) -> Path:
    """Path of the runtime credential, if present and unexpired. Never the admin kubeconfig."""
    path, meta = cfg.harness_kubeconfig_path, cfg.harness_token_meta_path
    if not path.is_file() or not meta.is_file():
        raise CredentialError(
            f"harness credential missing ({path.name}); run `make cluster-up`."
            " Runtime never falls back to the admin kubeconfig."
        )
    try:
        expires_at = int(json.loads(meta.read_text())["expires_at"])
    except Exception as exc:
        raise CredentialError(
            "harness token metadata is unreadable; run `make cluster-up`"
        ) from exc
    if now() >= expires_at:
        raise CredentialError(
            "harness token has expired; recreate the cluster or re-issue the token"
            " (refresh is deferred to before Milestone 14, decision 0009)."
            " Runtime never falls back to the admin kubeconfig."
        )
    return path


def kubeconfig_for(cfg: ClusterConfig, phase: Phase) -> Path:
    if phase is Phase.RUNTIME:
        return harness_credential(cfg)
    if not cfg.kubeconfig_path.is_file():
        raise CredentialError(f"admin kubeconfig missing ({cfg.kubeconfig_path.name})")
    return cfg.kubeconfig_path


# --- kubectl -------------------------------------------------------------------------

Runner = Callable[..., subprocess.CompletedProcess]


class Kubectl:
    """kubectl with an explicit kubeconfig and context on every call."""

    def __init__(
        self,
        cfg: ClusterConfig,
        phase: Phase,
        runner: Runner = subprocess.run,
        kubeconfig: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.phase = phase
        self._run = runner
        self.kubeconfig = kubeconfig if kubeconfig is not None else kubeconfig_for(cfg, phase)

    def base_args(self) -> list[str]:
        return ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", self.cfg.kube_context]

    def _exec(self, args: list[str], stdin: str | None = None, timeout: float = 120) -> str:
        proc = self._run(
            self.base_args() + args,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            detail = err[-1] if err else f"exit {proc.returncode}"
            if "Unauthorized" in detail or "must be logged in" in detail:
                raise CredentialError(
                    f"{self.phase.value} credential rejected by the API server ({detail})."
                    " Runtime never falls back to the admin kubeconfig."
                )
            raise CommandError(f"kubectl {args[0]} failed: {detail}")
        return proc.stdout

    def read(self, *args: str, timeout: float = 120) -> str:
        """Run a read-only kubectl command."""
        verb = args[0] if args else ""
        allowed_sub = _READ_SUBCOMMANDS.get(verb)
        if verb not in _READ_VERBS and not (
            allowed_sub and len(args) > 1 and args[1] in allowed_sub
        ):
            raise GuardError(f"kubectl {verb!r} is not a read-only command")
        return self._exec(list(args), timeout=timeout)

    def get_json(self, *args: str) -> dict:
        return json.loads(self.read("get", *args, "-o", "json"))

    def apply(
        self,
        objects: Iterable[dict],
        cluster_scope: frozenset[tuple[str, str]] = frozenset(),
        cluster_scoped_kinds: frozenset[str] = frozenset(),
    ) -> None:
        """Server-side apply after checking every object against the guard."""
        import yaml

        objects = list(objects)
        for obj in objects:
            kind, meta = obj["kind"], obj.get("metadata", {})
            namespace = None if kind in cluster_scoped_kinds else meta.get("namespace")
            if namespace is None and kind not in cluster_scoped_kinds:
                raise GuardError(f"{kind}/{meta.get('name')} has no namespace; refusing to default")
            check_object(
                self.cfg,
                self.phase,
                kind=kind,
                name=meta["name"],
                namespace=namespace,
                cluster_scope=cluster_scope,
            )
        self._exec(
            ["apply", "--server-side", "--field-manager=safeops", "-f", "-"],
            stdin=yaml.safe_dump_all(objects, sort_keys=False),
        )

    def create_token(self, service_account: str, namespace: str, duration_s: int) -> str:
        """Issue a time-limited TokenRequest token. Bootstrap only; the token is never printed."""
        if self.phase is not Phase.BOOTSTRAP:
            raise GuardError("only bootstrap may issue harness tokens")
        check_object(
            self.cfg, self.phase, kind="ServiceAccount", name=service_account, namespace=namespace
        )
        return self._exec(
            ["create", "token", service_account, "-n", namespace, f"--duration={duration_s}s"]
        ).strip()

    def raw(self, method: str, path: str, body: dict | None = None) -> str:
        """Raw API call. Reached only through WorkloadProxy and bootstrap health checks.

        Defense in depth: at runtime, raw calls may only target the workload generator's
        proxy path. Bootstrap may only GET.
        """
        if self.phase is Phase.RUNTIME:
            prefix = (
                f"/api/v1/namespaces/{self.cfg.workload_namespace}"
                f"/services/{self.cfg.workload_service}/proxy/"
            )
            if not path.startswith(prefix) or ".." in path:
                raise GuardError("runtime raw calls may only reach the workload generator proxy")
        elif method != "GET":
            raise GuardError("bootstrap raw calls are GET only")
        if method == "GET":
            return self._exec(["get", "--raw", path])
        if method == "PUT":
            return self._exec(["replace", "--raw", path, "-f", "-"], stdin=json.dumps(body or {}))
        raise GuardError(f"raw {method} is not supported")


# --- workload generator proxy --------------------------------------------------------


class WorkloadProxy:
    """The harness's direct path to the workload generator (decision 0009).

    The capability check here is separate from the object guard. A bug in a caller must
    not be able to reach any other Service, namespace, or endpoint.
    """

    OPERATIONS = {"stats": ("GET", "/stats"), "control": ("PUT", "/control")}

    def __init__(self, cfg: ClusterConfig, kubectl: Kubectl | None = None) -> None:
        self.cfg = cfg
        self._kubectl = kubectl

    @property
    def kubectl(self) -> Kubectl:
        if self._kubectl is None:
            self._kubectl = Kubectl(self.cfg, Phase.RUNTIME)
        return self._kubectl

    def path(self, namespace: str, service: str, operation: str, query: str = "") -> str:
        if namespace != self.cfg.workload_namespace:
            raise GuardError(f"workload proxy may not reach namespace {namespace!r}")
        if service != self.cfg.workload_service:
            raise GuardError(f"workload proxy may not reach Service {service!r}")
        if operation not in self.OPERATIONS:
            raise GuardError(f"workload proxy operation {operation!r} is not stats or control")
        if query and (not query.startswith("?") or "/" in query or "#" in query):
            raise GuardError("workload proxy query must be a plain '?key=value' string")
        endpoint = self.OPERATIONS[operation][1]
        # The plain Service name, so the RBAC resourceName matches exactly.
        return f"/api/v1/namespaces/{namespace}/services/{service}/proxy{endpoint}{query}"

    def _call(self, operation: str, body: dict | None = None, query: str = "") -> dict:
        method = self.OPERATIONS[operation][0]
        path = self.path(self.cfg.workload_namespace, self.cfg.workload_service, operation, query)
        out = self.kubectl.raw(method, path, body)
        return json.loads(out) if out.strip() else {}

    def stats(self, window_id: str | None = None) -> dict:
        query = ""
        if window_id is not None:
            if not window_id.isalnum():
                raise GuardError("window id must be alphanumeric")
            query = f"?window={window_id}"
        return self._call("stats", query=query)

    def control(self, body: dict) -> dict:
        return self._call("control", body=body)
