"""The executor's concrete Kubernetes transports (design §4, §11.6; decision 0025 D§11.3,
D§11.6, D§11.7, D§14, M8B-2 amendment).

- **KubernetesWriterTransport**: the safeops-executor identity (Phase.EXECUTOR), patch only.
  It owns everything between a PreparedWrite and its semantic WriteReply: preflight, the
  credential, the durable `before_write` audit entry, the one guarded `kubectl patch`, the
  reading of what kubectl did, and the `after_write` entry.
- **KubernetesReaderTransport**: the executor's reads through safeops-reader (Phase.READER):
  one kubectl process per call, FOUND / ABSENT / UNAVAILABLE.

Neither builds a client or resolves a credential when constructed: build_runner stays free
of credentials and of the cluster. The client is built on first use.

**Credential failures are three different things** (never one broad catch):
- the configured credential is missing or expired (CredentialUnavailable, detected locally):
  the environment couldn't serve a request the executor was entitled to make. The write is
  NOT_ATTEMPTED (D§11.3);
- another identity's credential, or an existing but unusable kubeconfig: a harness bug,
  raised loud from preflight (nothing sent);
- the context's API server isn't this cluster's: GuardError, raised loud from preflight.

**After a patch may have been sent**, the WriteReply is already determined and nothing may
replace it: an `after_write` audit failure of any non-interrupt kind is dropped
(audit.keep_evidence), never reclassified, never NOT_ATTEMPTED, never a retry.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable

from safeops.cluster import (
    EXECUTOR_PATCH_TYPES,
    EXECUTOR_RESOURCES,
    ClusterConfig,
    CommandError,
    CredentialError,
    CredentialUnavailable,
    GuardError,
    Kubectl,
    Phase,
    ProcessResult,
    Runner,
    check_object,
    executor_credential,
)
from safeops.executor.audit import AuditPersistenceError, ExecutorAudit, keep_evidence
from safeops.executor.transport import (
    PreparedWrite,
    ReadOutcome,
    ReadReply,
    WriteClass,
    WriteReply,
)

# One write: kubectl's own request timeout, and the process bound a little above it.
WRITE_TIMEOUT_S = 20
EXECUTOR_SERVICE_ACCOUNT = "safeops-executor"
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")

# kubectl -v=6 logs one line per HTTP response: the method, URL and status, never a header
# or a token. Both the legacy and the structured (klog) forms are read.
_LEGACY_RESPONSE = re.compile(r"\b(GET|PATCH|PUT|POST|DELETE) (https?://\S+) (\d{3})\b")
_STRUCTURED_VERB = re.compile(r'\bverb="([A-Z]+)"')
_STRUCTURED_URL = re.compile(r'\burl="([^"]+)"')
_STRUCTURED_STATUS = re.compile(r'\bstatus="(\d{3})\b')
_KLOG_LINE = re.compile(r"^[IWEF]\d{4} ")


def _responses(stderr: str) -> list[tuple[str, str, int]]:
    """(method, url, HTTP status) for every response kubectl logged, in order."""
    out = []
    for line in stderr.splitlines():
        verb, url, status = (
            _STRUCTURED_VERB.search(line),
            _STRUCTURED_URL.search(line),
            _STRUCTURED_STATUS.search(line),
        )
        if verb and url and status:
            out.append((verb.group(1), url.group(1), int(status.group(1))))
            continue
        legacy = _LEGACY_RESPONSE.search(line)
        if legacy:
            out.append((legacy.group(1), legacy.group(2), int(legacy.group(3))))
    return out


def _object_path(request: PreparedWrite) -> str:
    plural = EXECUTOR_RESOURCES[request.kind].split(".", 1)[0]
    return f"/namespaces/{request.namespace}/{plural}/{request.name}"


def patch_status(stderr: str, request: PreparedWrite) -> int | None:
    """The HTTP status of the PATCH of this object, from kubectl's request log, or None if
    no such response was logged (the PATCH was never answered, or never sent)."""
    path = _object_path(request)
    found = None
    for method, url, status in _responses(stderr):
        if method == "PATCH" and url.split("?", 1)[0].endswith(path):
            found = status
    return found


def _message(process: ProcessResult) -> str:
    """kubectl's own error text (its last non-log stderr line), for the diagnostic."""
    lines = [
        ln.strip()
        for ln in process.stderr.splitlines()
        if ln.strip() and not _KLOG_LINE.match(ln.strip())
    ]
    return lines[-1] if lines else f"exit {process.returncode}"


def _returned_object(stdout: str) -> dict | None:
    try:
        obj = json.loads(stdout)
    except ValueError:
        return None
    return obj if type(obj) is dict else None


def classify_patch(process: ProcessResult, request: PreparedWrite) -> WriteReply:
    """What one `kubectl patch` did, as one of the five WriteClass values (D§11.3). Total."""
    try:
        return _classify(process, request)
    except Exception as exc:  # a bug in reading the output: the write may have landed
        return WriteReply(WriteClass.AMBIGUOUS, diagnostic=f"unreadable outcome: {exc}")


def _classify(process: ProcessResult, request: PreparedWrite) -> WriteReply:
    if process.spawn_error is not None:  # kubectl never started: nothing was sent
        return WriteReply(
            WriteClass.NOT_ATTEMPTED, diagnostic=f"kubectl did not start: {process.spawn_error}"
        )
    if process.timed_out:
        return WriteReply(WriteClass.AMBIGUOUS, diagnostic="kubectl patch timed out")
    status = patch_status(process.stderr, request)
    if status is None:
        if process.returncode == 0:
            obj = _returned_object(process.stdout)
            if obj is not None:
                return WriteReply(WriteClass.SUCCESS, obj, "exit 0 (no response status logged)")
            return WriteReply(WriteClass.AMBIGUOUS, diagnostic="exit 0, output not an object")
        refused = [s for _, _, s in _responses(process.stderr) if s in (401, 403)]
        if refused:  # discovery refused the identity: the PATCH was never sent
            return WriteReply(
                WriteClass.DEFINITE_FAILURE,
                diagnostic=f"HTTP {refused[-1]} before the PATCH was sent: {_message(process)}",
            )
        return WriteReply(
            WriteClass.AMBIGUOUS, diagnostic=f"no PATCH response: {_message(process)}"
        )
    if 200 <= status < 300:
        obj = _returned_object(process.stdout) if process.returncode == 0 else None
        if obj is not None:
            return WriteReply(WriteClass.SUCCESS, obj, f"HTTP {status}")
        return WriteReply(
            WriteClass.AMBIGUOUS,
            diagnostic=f"HTTP {status}, but exit {process.returncode} or no returned object",
        )
    detail = f"HTTP {status}: {_message(process)}"
    if status == 422:
        return WriteReply(WriteClass.INVALID_ACTION, diagnostic=detail)
    if 400 <= status < 500:
        return WriteReply(WriteClass.DEFINITE_FAILURE, diagnostic=detail)
    return WriteReply(WriteClass.AMBIGUOUS, diagnostic=detail)


def _check_request(cfg: ClusterConfig, request: object) -> PreparedWrite:
    """A request the executor phase may send, checked with check_object directly (never
    _check_objects, which would ask the API server for its kinds). Raises GuardError."""
    if not isinstance(request, PreparedWrite):
        raise GuardError(f"the writer sends PreparedWrite requests, not {type(request).__name__}")
    check_object(
        cfg, Phase.EXECUTOR, kind=request.kind, name=request.name, namespace=request.namespace
    )
    if type(request.name) is not str or not _NAME.fullmatch(request.name):
        raise GuardError(f"invalid object name {request.name!r}")
    if request.patch_type.value not in EXECUTOR_PATCH_TYPES:
        raise GuardError(f"patch type {request.patch_type!r} is not allowed")
    try:
        body = json.loads(request.body_json)
    except ValueError as exc:
        raise GuardError("the patch body is not JSON") from exc
    if type(body) is not (list if request.patch_type.value == "json" else dict):
        raise GuardError(f"a {request.patch_type.value} patch body has the wrong JSON shape")
    return request


class KubernetesWriterTransport:
    """The safeops-executor writer (module docstring). It takes no kubeconfig: it only ever
    uses the configured executor credential."""

    def __init__(
        self,
        cfg: ClusterConfig,
        audit: ExecutorAudit,
        *,
        runner: Runner = subprocess.run,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(cfg, ClusterConfig):
            raise TypeError("the writer needs the loaded ClusterConfig")
        if not isinstance(audit, ExecutorAudit):
            raise TypeError(f"{type(audit).__name__} is not an ExecutorAudit")
        self.cfg = cfg
        self.audit = audit
        self._runner = runner
        self._now = now
        self._kubectl: Kubectl | None = None
        self._preflighted: frozenset[PreparedWrite] = frozenset()

    @property
    def identity(self) -> str:
        return f"system:serviceaccount:{self.cfg.system_namespace}:{EXECUTOR_SERVICE_ACCOUNT}"

    def preflight(self, requests: tuple[PreparedWrite, ...]) -> None:
        """Every loud pre-send refusal, before the first write (D§11.3): request shape and
        scope (GuardError), and, when the configured credential is available, the client's
        identity (wrong identity or an unusable kubeconfig: CredentialError; another cluster:
        GuardError). A missing or expired credential is not raised: write reports it."""
        if type(requests) is not tuple or not requests:
            raise GuardError("preflight takes a non-empty tuple of requests")
        checked = frozenset(_check_request(self.cfg, r) for r in requests)
        try:
            path = executor_credential(self.cfg, self._now)
        except CredentialUnavailable:
            path = None
        if path is not None and self._kubectl is None:
            client = Kubectl(self.cfg, Phase.EXECUTOR, runner=self._runner, kubeconfig=path)
            client.verify_identity()
            self._kubectl = client  # only a verified client is kept
        self._preflighted = checked

    def write(self, request: PreparedWrite) -> WriteReply:
        if request not in self._preflighted:
            raise GuardError("the writer sends only requests it preflighted")
        try:
            self.audit.before_write(request, self.identity)  # an unbound sink raises: loud
        except AuditPersistenceError as exc:
            return self._after(
                request,
                WriteReply(
                    WriteClass.NOT_ATTEMPTED,
                    diagnostic=f"the audit before-entry could not be made durable: {exc}",
                ),
            )
        try:
            executor_credential(self.cfg, self._now)
        except CredentialUnavailable as exc:
            return self._after(request, WriteReply(WriteClass.NOT_ATTEMPTED, diagnostic=str(exc)))
        if self._kubectl is None:
            return self._after(
                request,
                WriteReply(
                    WriteClass.NOT_ATTEMPTED,
                    diagnostic="the executor credential was unavailable at preflight, so its"
                    " identity was never verified; not sent",
                ),
            )
        try:
            process = self._kubectl.patch(
                kind=request.kind,
                name=request.name,
                namespace=request.namespace,
                patch_type=request.patch_type.value,
                body_json=request.body_json,
                timeout_s=WRITE_TIMEOUT_S,
            )
        except GuardError:
            raise  # the client guard refused before running anything: a harness bug
        except Exception as exc:  # the patch process may have run
            reply = WriteReply(WriteClass.AMBIGUOUS, diagnostic=f"{type(exc).__name__}: {exc}")
        else:
            reply = classify_patch(process, request)
        return self._after(request, reply)

    def _after(self, request: PreparedWrite, reply: WriteReply) -> WriteReply:
        """The reply is determined; the after entry can't change or erase it."""
        keep_evidence(lambda: self.audit.after_write(request, reply))
        return reply


class KubernetesReaderTransport:
    """The executor's safeops-reader reads (D§11.7). One call is one kubectl process."""

    def __init__(self, cfg: ClusterConfig, *, runner: Runner = subprocess.run) -> None:
        if not isinstance(cfg, ClusterConfig):
            raise TypeError("the reader needs the loaded ClusterConfig")
        self.cfg = cfg
        self._runner = runner
        self._kubectl: Kubectl | None = None

    def _client(self) -> Kubectl:
        if self._kubectl is None:
            self._kubectl = Kubectl(self.cfg, Phase.READER, runner=self._runner)
        return self._kubectl

    def _check(self, namespace: str) -> None:
        if namespace != self.cfg.namespace:
            raise GuardError(f"the executor reads only {self.cfg.namespace!r}, not {namespace!r}")

    def _read(self, args: list[str], timeout_s: float) -> tuple[str | None, str | None]:
        """(stdout, None), or (None, why it couldn't be read). A guard refusal raises."""
        try:
            return self._client().read(*args, timeout=timeout_s), None
        except GuardError:
            raise
        except subprocess.TimeoutExpired:
            return None, f"the read did not finish in {timeout_s}s"
        except (CredentialError, CommandError, OSError, UnicodeDecodeError) as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def get(self, kind: str, name: str, namespace: str, timeout_s: float) -> ReadReply:
        resource = EXECUTOR_RESOURCES.get(kind)
        if resource is None:
            raise GuardError(f"the executor reader reads {', '.join(EXECUTOR_RESOURCES)} only")
        if type(name) is not str or not _NAME.fullmatch(name):
            raise GuardError(f"invalid object name {name!r}")
        self._check(namespace)
        out, why = self._read(
            ["get", resource, name, "-n", namespace, "-o", "json", "--ignore-not-found"],
            timeout_s,
        )
        if out is None:
            return ReadReply(ReadOutcome.UNAVAILABLE, diagnostic=why)
        if not out.strip():  # --ignore-not-found: a successful read that found nothing
            return ReadReply(ReadOutcome.ABSENT)
        obj = _returned_object(out)
        if obj is None:
            return ReadReply(ReadOutcome.UNAVAILABLE, diagnostic="the read returned no object")
        return ReadReply(ReadOutcome.FOUND, obj)

    def list(self, kind: str, namespace: str, timeout_s: float) -> ReadReply:
        if kind != "ReplicaSet":
            raise GuardError(f"the executor reader lists ReplicaSets only, not {kind!r}")
        self._check(namespace)
        # One unpaginated LIST: kubectl's own chunking is off, so nothing is stitched.
        out, why = self._read(
            ["get", "replicasets.apps", "-n", namespace, "-o", "json", "--chunk-size=0"],
            timeout_s,
        )
        if out is None:
            return ReadReply(ReadOutcome.UNAVAILABLE, diagnostic=why)
        obj = _returned_object(out)
        if obj is None or type(obj.get("items")) is not list:
            return ReadReply(ReadOutcome.UNAVAILABLE, diagnostic="the list returned no items")
        meta = obj.get("metadata") or {}
        if type(meta) is not dict or meta.get("continue") or meta.get("remainingItemCount"):
            return ReadReply(ReadOutcome.UNAVAILABLE, diagnostic="the list is incomplete")
        return ReadReply(ReadOutcome.FOUND, obj)
