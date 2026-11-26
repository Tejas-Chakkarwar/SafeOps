"""Cluster identity, the phase-aware context guard, and the only way SafeOps code runs
kubectl (decisions 0002, 0006, 0009).

- Every kubectl call passes --kubeconfig and --context from config/cluster.yaml.
  `kubectl config current-context` is never read.
- The phase picks the credential. Runtime always uses the experiment-controller token
  and never falls back to the admin kubeconfig. Bootstrap uses the admin kubeconfig
  that kind wrote, and only safeops.bootstrap may ask for it
  (tests/test_cluster_guard.py enforces that). Reader is SafeOps' read-only identity,
  safeops-reader (design §4, Milestone 6): its own token, never the harness or admin
  credential, no mutation at all, and raw calls only to Prometheus' query API. It serves
  the agent's tools and the executor's pre-write reads and reconciliation (decision 0025
  D§11.7).
- Executor is SafeOps' write identity, safeops-executor (design §4, Milestone 8; decision
  0025 D§14): its own token, never another identity's credential, and exactly one capability,
  `Kubectl.patch` on a Deployment, Service or ConfigMap in the testbed namespace. It makes no
  read of its own (no get, list, watch, api-resources, auth or raw call), and no other
  mutation (no apply, create, replace, delete, token or subresource).
- Kubernetes-object mutations go through `check_object`. Runtime allows the testbed
  namespace only; executor the testbed namespace and its three kinds only; bootstrap allows
  its namespaces plus an exactly recorded set of cluster-scoped objects
  (config/bootstrap_scope.yaml).
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
# through a dedicated, guarded method instead. Verbs with subcommands are limited to the
# listed read-only ones (`auth reconcile` writes RBAC, `rollout restart` mutates).
_READ_VERBS = frozenset({"get", "wait", "api-resources", "version", "logs"})
_READ_SUBCOMMANDS = {"rollout": frozenset({"status"}), "auth": frozenset({"can-i"})}

# Flags that would override the explicit kubeconfig, context or identity. kubectl uses the
# *last* occurrence of a flag, so a caller-supplied one silently wins over ours. They are
# refused in any caller argument, in both `--flag value` and `--flag=value` form.
# `--raw` is refused too, so raw API paths can only go through `Kubectl.raw` and its check.
_FORBIDDEN_FLAGS = frozenset(
    {
        "--kubeconfig",
        "--context",
        "--cluster",
        "--user",
        "--server",
        "-s",
        "--token",
        "--as",
        "--as-group",
        "--as-uid",
        "--username",
        "--password",
        "--certificate-authority",
        "--client-certificate",
        "--client-key",
        "--insecure-skip-tls-verify",
        "--tls-server-name",
        "--raw",
    }
)

# Built-in cluster-scoped kinds. `Kubectl` adds whatever else the live API server reports
# as cluster-scoped (`kubectl api-resources --namespaced=false`), so classification never
# depends on the caller, and a cluster-scoped custom kind is not mistaken for a namespaced one.
CLUSTER_SCOPED_KINDS = frozenset(
    {
        "APIService",
        "CertificateSigningRequest",
        "ClusterRole",
        "ClusterRoleBinding",
        "CSIDriver",
        "CSINode",
        "CustomResourceDefinition",
        "FlowSchema",
        "IngressClass",
        "MutatingAdmissionPolicy",
        "MutatingAdmissionPolicyBinding",
        "MutatingWebhookConfiguration",
        "Namespace",
        "Node",
        "PersistentVolume",
        "PriorityClass",
        "PriorityLevelConfiguration",
        "RuntimeClass",
        "StorageClass",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "ValidatingWebhookConfiguration",
        "VolumeAttachment",
    }
)


def _check_caller_args(args: Iterable[str]) -> None:
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in _FORBIDDEN_FLAGS:
            raise GuardError(
                f"argument {flag!r} would override the explicit kubeconfig, context,"
                " identity or raw path; refusing"
            )


class GuardError(Exception):
    """A command would act outside its phase's scope."""


class CredentialError(Exception):
    """The credential for a phase is missing, invalid, or expired."""


class CredentialUnavailable(CredentialError):
    """The configured credential file is absent, or its token has expired: detected locally,
    before any command runs. Distinct from a credential that exists but can't be used (a
    corrupt kubeconfig or unreadable token metadata) and from another identity's credential,
    which stay plain CredentialErrors."""


class CommandError(Exception):
    """A kubectl, helm or kind command failed."""


class Phase(Enum):
    RUNTIME = "runtime"
    BOOTSTRAP = "bootstrap"
    READER = "reader"  # safeops-reader: tools (M6) and executor reads (M8); never mutates
    EXECUTOR = "executor"  # safeops-executor (M8): patch only, three kinds, testbed only


# The only kinds the executor phase may patch (decision 0025 D§11.1 E1, D§14), and the
# kubectl resource for each: fully qualified, so no discovery shortcut can pick another group.
EXECUTOR_RESOURCES = {
    "Deployment": "deployments.apps",
    "Service": "services",
    "ConfigMap": "configmaps",
}
EXECUTOR_PATCH_TYPES = frozenset({"json", "merge", "strategic"})


@dataclass(frozen=True)
class ClusterConfig:
    cluster_name: str
    kube_context: str
    kind_node_image: str
    kubeconfig_path: Path
    harness_kubeconfig_path: Path
    harness_token_meta_path: Path
    reader_kubeconfig_path: Path
    reader_token_meta_path: Path
    executor_kubeconfig_path: Path
    executor_token_meta_path: Path
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
    def state_dir(self) -> Path:
        """The gitignored local state directory (.safeops/). Code that only needs a place
        for state files uses this, never the admin kubeconfig path itself."""
        return self.kubeconfig_path.parent

    @property
    def bootstrap_namespaces(self) -> tuple[str, ...]:
        return (
            self.namespace,
            self.system_namespace,
            self.workload_namespace,
            self.monitoring_namespace,
            self.chaos_namespace,
        )


_PATH_KEYS = (
    "kubeconfig_path",
    "harness_kubeconfig_path",
    "harness_token_meta_path",
    "reader_kubeconfig_path",
    "reader_token_meta_path",
    "executor_kubeconfig_path",
    "executor_token_meta_path",
)
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

    namespace=None means a cluster-scoped object. Every phase has its own explicit branch:
    none falls through to bootstrap's wider scope.
    """
    if phase is Phase.READER:
        raise GuardError(f"the reader identity never mutates ({kind}/{name})")
    if phase is Phase.EXECUTOR:
        if namespace is None:
            raise GuardError(f"the executor may not mutate cluster-scoped {kind}/{name}")
        if kind not in EXECUTOR_RESOURCES:
            raise GuardError(
                f"the executor may patch only {', '.join(EXECUTOR_RESOURCES)}, not {kind}/{name}"
            )
        if namespace != cfg.namespace:
            raise GuardError(
                f"executor may not mutate {kind}/{name} in namespace {namespace!r}"
                f" (allowed: {cfg.namespace})"
            )
        return
    if phase not in (Phase.RUNTIME, Phase.BOOTSTRAP):
        raise GuardError(f"unknown phase {phase!r}")
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


def _token_credential(path: Path, meta: Path, who: str, now: Callable[[], float]) -> Path:
    if not path.is_file() or not meta.is_file():
        raise CredentialUnavailable(
            f"{who} credential missing ({path.name}); run `make cluster-up`."
            " Runtime never falls back to the admin kubeconfig."
        )
    try:
        expires_at = int(json.loads(meta.read_text())["expires_at"])
    except Exception as exc:
        raise CredentialError(f"{who} token metadata is unreadable; run `make cluster-up`") from exc
    if now() >= expires_at:
        raise CredentialUnavailable(
            f"{who} token has expired; recreate the cluster or re-issue the token"
            " (refresh is deferred to before Milestone 14, decision 0009)."
            " Runtime never falls back to the admin kubeconfig."
        )
    return path


def harness_credential(cfg: ClusterConfig, now: Callable[[], float] = time.time) -> Path:
    """Path of the runtime credential, if present and unexpired. Never the admin kubeconfig."""
    return _token_credential(
        cfg.harness_kubeconfig_path, cfg.harness_token_meta_path, "harness", now
    )


def reader_credential(cfg: ClusterConfig, now: Callable[[], float] = time.time) -> Path:
    """Path of safeops-reader's credential (Milestone 6), if present and unexpired. Never the
    harness or admin credential."""
    return _token_credential(cfg.reader_kubeconfig_path, cfg.reader_token_meta_path, "reader", now)


def executor_credential(cfg: ClusterConfig, now: Callable[[], float] = time.time) -> Path:
    """Path of safeops-executor's credential (Milestone 8), if present and unexpired. Never the
    harness, reader or admin credential. Missing or expired is CredentialUnavailable."""
    return _token_credential(
        cfg.executor_kubeconfig_path, cfg.executor_token_meta_path, "executor", now
    )


def kubeconfig_for(cfg: ClusterConfig, phase: Phase) -> Path:
    """The one credential each phase uses. An explicit branch per phase: no phase falls
    through to another identity's credential, and only bootstrap reaches the admin one."""
    if phase is Phase.RUNTIME:
        return harness_credential(cfg)
    if phase is Phase.READER:
        return reader_credential(cfg)
    if phase is Phase.EXECUTOR:
        return executor_credential(cfg)
    if phase is not Phase.BOOTSTRAP:
        raise GuardError(f"no credential is defined for phase {phase!r}")
    if not cfg.kubeconfig_path.is_file():
        raise CredentialError(f"admin kubeconfig missing ({cfg.kubeconfig_path.name})")
    return cfg.kubeconfig_path


# --- kubectl -------------------------------------------------------------------------


def decode_raw(data: bytes | str | None) -> str:
    """Bytes from a raw kubectl call as text: UTF-8 with errors="replace", whatever the
    process locale (decision 0023, second closure amendment). It never raises, and it is
    deterministic: the same bytes always give the same text, with no surrogate code points
    (surrogateescape would make canonical JSON fail; ignore would delete evidence). A str
    passes through unchanged (test doubles that return text)."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return bytes(data).decode("utf-8", errors="replace")


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class ProcessResult:
    """What one guarded kubectl process did, uninterpreted (the executor's adapter reads it).
    Exactly one of: it never started (spawn_error), it timed out (timed_out), or it exited
    (returncode). Output is decoded text; it never holds a credential (kubectl prints none)."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    spawn_error: str | None = None


class Kubectl:
    """kubectl with an explicit kubeconfig and context on every call."""

    def __init__(
        self,
        cfg: ClusterConfig,
        phase: Phase,
        runner: Runner = subprocess.run,
        kubeconfig: Path | None = None,
        verify_identity: bool = True,
    ) -> None:
        self.cfg = cfg
        self.phase = phase
        self._run = runner
        if kubeconfig is not None and phase in (Phase.RUNTIME, Phase.READER, Phase.EXECUTOR):
            # Runtime never uses the admin credential, however it is passed in (review C1);
            # the reader (Milestone 6) and the executor (Milestone 8) never use anyone's
            # credential but their own.
            refused = {cfg.kubeconfig_path.resolve(): "the admin kubeconfig"}
            if phase in (Phase.READER, Phase.EXECUTOR):
                refused[cfg.harness_kubeconfig_path.resolve()] = "the harness kubeconfig"
            if phase is Phase.READER:
                refused[cfg.executor_kubeconfig_path.resolve()] = "the executor kubeconfig"
            if phase is Phase.EXECUTOR:
                refused[cfg.reader_kubeconfig_path.resolve()] = "the reader kubeconfig"
            if kubeconfig.resolve() in refused:
                raise CredentialError(
                    f"{phase.value} was handed {refused[kubeconfig.resolve()]}; it never falls"
                    " back to another identity's credential"
                )
        self.kubeconfig = kubeconfig if kubeconfig is not None else kubeconfig_for(cfg, phase)
        self._cluster_scoped: frozenset[str] | None = None
        self._identity_verified = not verify_identity

    def base_args(self) -> list[str]:
        return ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", self.cfg.kube_context]

    def verify_identity(self) -> None:
        """Check the context's API server now, before any command (see _verify_identity)."""
        self._verify_identity()

    def _verify_identity(self) -> None:
        """The context must really be the SafeOps kind cluster (review C3).

        The context name alone proves nothing: a stale or copied kubeconfig could call
        anything `kind-safeops`. Before the first command, check that the context's API
        server is the loopback port Docker publishes for this cluster's kind node.
        """
        if self._identity_verified:
            return
        import yaml

        try:
            kc = yaml.safe_load(Path(self.kubeconfig).read_text())
            ctx = next(c["context"] for c in kc["contexts"] if c["name"] == self.cfg.kube_context)
            server = next(
                c["cluster"]["server"] for c in kc["clusters"] if c["name"] == ctx["cluster"]
            )
        except Exception as exc:
            raise CredentialError(
                f"{Path(self.kubeconfig).name} has no usable {self.cfg.kube_context!r} context"
            ) from exc
        node = f"{self.cfg.cluster_name}-control-plane"
        proc = self._run(
            ["docker", "port", node, "6443/tcp"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        published = {
            f"https://{line.strip()}"
            for line in (proc.stdout or "").splitlines()
            if line.strip().startswith("127.0.0.1:")
        }
        if proc.returncode != 0 or server not in published:
            raise GuardError(
                f"context {self.cfg.kube_context!r} points at {server}, which is not the API"
                f" server Docker publishes for kind node {node!r}; refusing to run anything"
            )
        self._identity_verified = True

    def _exec(
        self, args: list[str], stdin: str | None = None, timeout: float = 120, *, raw: bool = False
    ) -> str:
        r"""Run kubectl. `raw` takes the output as bytes and decodes it here (decode_raw), with
        no newline translation: text mode's universal newlines turn "\r" and "\r\n" into
        "\n" before the caller sees them (decision 0023, second closure amendment). A raw
        call takes no stdin, so a str can never meet a binary pipe."""
        self._verify_identity()
        if raw and stdin is not None:
            raise GuardError("a raw kubectl call takes no stdin")
        proc = self._run(
            self.base_args() + args,
            input=stdin,
            capture_output=True,
            text=not raw,
            timeout=timeout,
            check=False,
        )
        stdout, stderr = proc.stdout, proc.stderr
        if raw:
            stdout, stderr = decode_raw(stdout), decode_raw(stderr)
        if proc.returncode != 0:
            err = (stderr or "").strip().splitlines()
            detail = err[-1] if err else f"exit {proc.returncode}"
            if "Unauthorized" in detail or "must be logged in" in detail:
                raise CredentialError(
                    f"{self.phase.value} credential rejected by the API server ({detail})."
                    " Runtime never falls back to the admin kubeconfig."
                )
            raise CommandError(f"kubectl {args[0]} failed: {detail}")
        return stdout

    def _refuse_executor(self, what: str) -> None:
        """The executor phase has one capability, `patch`; everything else is refused here,
        before any command runs."""
        if self.phase is Phase.EXECUTOR:
            raise GuardError(f"the executor identity may only patch; {what} is refused")

    def read(self, *args: str, timeout: float = 120) -> str:
        """Run a read-only kubectl command. Caller arguments can't override the identity."""
        self._refuse_executor("a read")
        self._check_read(args)
        return self._exec(list(args), timeout=timeout)

    def read_raw(self, *args: str, timeout: float = 120) -> str:
        r"""read(), behind the same guard, identity check, timeout and errors, with the output
        taken as bytes and decoded explicitly (see _exec). For `kubectl logs`, whose output
        is application data: every character except kubectl's own "\n" must arrive as is."""
        self._refuse_executor("a read")
        self._check_read(args)
        return self._exec(list(args), timeout=timeout, raw=True)

    @staticmethod
    def _check_read(args: tuple[str, ...]) -> None:
        verb = args[0] if args else ""
        allowed_sub = _READ_SUBCOMMANDS.get(verb)
        if allowed_sub is not None:
            if len(args) < 2 or args[1] not in allowed_sub:
                raise GuardError(f"kubectl {' '.join(args[:2])!r} is not a read-only command")
        elif verb not in _READ_VERBS:
            raise GuardError(f"kubectl {verb!r} is not a read-only command")
        _check_caller_args(args)

    def get_json(self, *args: str) -> dict:
        return json.loads(self.read("get", *args, "-o", "json"))

    def cluster_scoped_kinds(self) -> frozenset[str]:
        """Built-in cluster-scoped kinds plus every kind the live API server reports as
        cluster-scoped. Asked once per Kubectl; the caller never supplies it."""
        self._refuse_executor("API discovery (api-resources)")
        if self._cluster_scoped is None:
            out = self._exec(["api-resources", "--namespaced=false", "--no-headers"])
            live = {line.split()[-1] for line in out.splitlines() if line.strip()}
            self._cluster_scoped = CLUSTER_SCOPED_KINDS | frozenset(live)
        return self._cluster_scoped

    def _check_objects(
        self, objects: list[dict], cluster_scope: frozenset[tuple[str, str]]
    ) -> None:
        """Guard every object before anything runs. Scope comes from the kind, never from
        the object: a cluster-scoped kind is cluster-scoped even if it carries a
        metadata.namespace (Kubernetes ignores that field for such kinds)."""
        cluster_kinds = self.cluster_scoped_kinds()
        for obj in objects:
            kind, meta = obj["kind"], obj.get("metadata", {})
            if kind in cluster_kinds:
                namespace = None
            else:
                namespace = meta.get("namespace")
                if namespace is None:
                    raise GuardError(
                        f"{kind}/{meta.get('name')} has no namespace; refusing to default"
                    )
            check_object(
                self.cfg,
                self.phase,
                kind=kind,
                name=meta["name"],
                namespace=namespace,
                cluster_scope=cluster_scope,
            )

    def _mutate(self, verb_args: list[str], objects, cluster_scope) -> None:
        import yaml

        self._refuse_executor(f"kubectl {verb_args[0]}")
        objects = list(objects)
        self._check_objects(objects, cluster_scope)
        self._exec(verb_args + ["-f", "-"], stdin=yaml.safe_dump_all(objects, sort_keys=False))

    def apply(
        self, objects: Iterable[dict], cluster_scope: frozenset[tuple[str, str]] = frozenset()
    ) -> None:
        """Server-side apply after checking every object against the guard."""
        self._mutate(["apply", "--server-side", "--field-manager=safeops"], objects, cluster_scope)

    def create(
        self, objects: Iterable[dict], cluster_scope: frozenset[tuple[str, str]] = frozenset()
    ) -> None:
        """Create objects after checking every one against the guard."""
        self._mutate(["create"], objects, cluster_scope)

    def create_returning(self, obj: dict, timeout: float = 60) -> dict:
        """Create one object after the guard check and return it as the API server stored
        it (server-populated fields included): the clock probe reads its creationTimestamp
        (decision 0017 §6)."""
        import yaml

        self._refuse_executor("kubectl create")
        self._check_objects([obj], frozenset())
        out = self._exec(
            ["create", "-o", "json", "-f", "-"], stdin=yaml.safe_dump(obj), timeout=timeout
        )
        return json.loads(out)

    def replace(
        self, objects: Iterable[dict], cluster_scope: frozenset[tuple[str, str]] = frozenset()
    ) -> None:
        """Replace (PUT) whole objects after the guard check: fields set by anyone else are
        dropped, which server-side apply would keep."""
        self._mutate(["replace"], objects, cluster_scope)

    def delete(
        self, resource: str, kind: str, name: str, namespace: str, timeout_s: int = 180
    ) -> None:
        """Delete one namespaced object and wait for it (and, foreground, its dependents) to
        be gone. `resource` is the kubectl resource name (e.g. deployment,
        networkchaos.chaos-mesh.org); `kind` is what the guard checks."""
        import re

        self._refuse_executor("kubectl delete")
        if not re.fullmatch(r"[a-z0-9]+(\.[a-z0-9-]+)*", resource):
            raise GuardError(f"invalid resource name {resource!r}")
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name):
            raise GuardError(f"invalid object name {name!r}")
        if kind in self.cluster_scoped_kinds():
            raise GuardError(f"delete of cluster-scoped {kind}/{name} is not supported here")
        check_object(self.cfg, self.phase, kind=kind, name=name, namespace=namespace)
        self._exec(
            [
                "delete",
                f"{resource}/{name}",
                "-n",
                namespace,
                "--ignore-not-found",
                "--wait=true",
                "--cascade=foreground",
                f"--timeout={timeout_s}s",
            ],
            timeout=timeout_s + 30,
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

    def raw(self, method: str, path: str, body: dict | None = None, timeout: float = 120) -> str:
        """Raw API call. Reached only through WorkloadProxy, bootstrap health checks and the
        reader's Prometheus queries.

        Defense in depth: at runtime, raw calls may only target the workload generator's
        proxy path. Bootstrap may only GET. The reader may only GET Prometheus' query API
        through its Service proxy (Milestone 6).

        Every path must be an absolute API path. A wrapper validates its own assumptions:
        without this, a bootstrap path shaped like a flag ("--kubeconfig=...") would land
        after the guard's own --kubeconfig and win, because kubectl takes the last one.
        """
        self._refuse_executor("a raw API call")
        if not path.startswith("/"):
            raise GuardError(f"raw path must start with '/', got {path[:40]!r}")
        if self.phase is Phase.RUNTIME:
            prefix = (
                f"/api/v1/namespaces/{self.cfg.workload_namespace}"
                f"/services/{self.cfg.workload_service}/proxy/"
            )
            if not path.startswith(prefix) or ".." in path:
                raise GuardError("runtime raw calls may only reach the workload generator proxy")
        elif self.phase is Phase.READER:
            prefix = (  # by port: the Service's port is named, so a bare name won't resolve
                f"/api/v1/namespaces/{self.cfg.monitoring_namespace}/services/"
                f"{self.cfg.prometheus_service}:{self.cfg.prometheus_service_port}/proxy/api/v1/"
            )
            rest = path[len(prefix) :] if path.startswith(prefix) else ""
            endpoint = rest.split("?", 1)[0]
            if method != "GET" or endpoint not in ("query", "query_range"):
                raise GuardError("reader raw calls may only GET Prometheus' query API")
            if ".." in path or "#" in path or "?" not in rest:
                raise GuardError("reader raw calls need a plain query string")
        elif self.phase is not Phase.BOOTSTRAP:
            raise GuardError(f"no raw call is defined for phase {self.phase!r}")
        elif method != "GET":
            raise GuardError("bootstrap raw calls are GET only")
        if method == "GET":
            return self._exec(["get", "--raw", path], timeout=timeout)
        if method == "PUT":
            return self._exec(
                ["replace", "--raw", path, "-f", "-"], stdin=json.dumps(body or {}), timeout=timeout
            )
        raise GuardError(f"raw {method} is not supported")

    def patch(
        self,
        *,
        kind: str,
        name: str,
        namespace: str,
        patch_type: str,
        body_json: str,
        timeout_s: float,
    ) -> ProcessResult:
        """The executor's one capability (decision 0025 D§14): `kubectl patch` of one object,
        returning the patched object as JSON, with kubectl's request log (-v=6: method, URL and
        HTTP status of each request; never a header or a token) on stderr for the adapter.

        Guarded before anything runs: the executor phase only; a structured kind, name,
        namespace, patch type and JSON body, never caller argv; the object checked with
        `check_object` directly. Never `_check_objects`: that asks the API server for its
        cluster-scoped kinds (`kubectl api-resources`), a read the executor must never make, and
        the executor's three kinds are fixed and namespaced.

        Never raises for a process outcome: exit status, timeout and a failure to start are
        returned as a ProcessResult. A guard refusal or an unusable identity raises (nothing
        was sent)."""
        import re

        if self.phase is not Phase.EXECUTOR:
            raise GuardError("only the executor identity patches through Kubectl.patch")
        resource = EXECUTOR_RESOURCES.get(kind)
        if resource is None:
            raise GuardError(f"the executor may not patch kind {kind!r}")
        if type(name) is not str or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
            raise GuardError(f"invalid object name {name!r}")
        if patch_type not in EXECUTOR_PATCH_TYPES:
            raise GuardError(f"patch type {patch_type!r} is not json, merge or strategic")
        try:
            parsed = json.loads(body_json)
        except (TypeError, ValueError) as exc:
            raise GuardError("the patch body is not JSON") from exc
        if type(parsed) is not (list if patch_type == "json" else dict):
            raise GuardError(f"a {patch_type} patch body has the wrong JSON shape")
        check_object(self.cfg, self.phase, kind=kind, name=name, namespace=namespace)
        self._verify_identity()
        args = [
            "patch",
            resource,
            name,
            "-n",
            namespace,
            "--type",
            patch_type,
            "-p",
            body_json,
            "-o",
            "json",
            "-v=6",
            f"--request-timeout={int(timeout_s)}s",
        ]
        try:
            proc = self._run(
                self.base_args() + args,
                capture_output=True,
                text=False,
                timeout=timeout_s + 5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ProcessResult(
                None, decode_raw(exc.stdout), decode_raw(exc.stderr), timed_out=True
            )
        except OSError as exc:  # kubectl never started: nothing was sent
            return ProcessResult(None, spawn_error=f"{type(exc).__name__}: {exc}")
        return ProcessResult(proc.returncode, decode_raw(proc.stdout), decode_raw(proc.stderr))


# --- workload generator proxy --------------------------------------------------------


class WorkloadProxy:
    """The harness's direct path to the workload generator (decision 0009).

    The capability check here is separate from the object guard. A bug in a caller must
    not be able to reach any other Service, namespace, or endpoint.
    """

    OPERATIONS = {"stats": ("GET", "/stats"), "control": ("PUT", "/control")}

    def __init__(self, cfg: ClusterConfig, kubectl: Kubectl | None = None) -> None:
        if kubectl is not None and kubectl.phase is not Phase.RUNTIME:
            # The evaluator path belongs to the harness identity only (review C2).
            raise GuardError("the workload proxy only runs with the runtime (harness) credential")
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
