"""The executor's concrete Kubernetes transports (design §4, §11.6; decision 0025 D§11.3,
D§11.6, D§11.7, D§14, M8B-2 amendment).

- **KubernetesWriterTransport**: the safeops-executor identity, patch only. It owns
  everything between a PreparedWrite and its semantic WriteReply: preflight, the credential,
  the durable `before_write` audit entry, the one direct HTTPS PATCH (safeops.kube_patch; not
  kubectl, which GETs the object first: M8B-2 STOP resolution), the classification of what
  came back, and the `after_write` entry.
- **KubernetesReaderTransport**: the executor's reads through safeops-reader (Phase.READER):
  one kubectl process per call, FOUND / ABSENT / UNAVAILABLE. Unchanged by the STOP
  resolution, and never routed through the direct PATCH.

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
from safeops.jsonutil import MalformedJSON, strict_json_object
from safeops.kube_patch import (
    EXECUTOR_SERVICE_ACCOUNT,
    ExecutorPatchClient,
    PatchExchange,
    SendState,
    https_connection,
)

# One write: the PATCH's socket timeout (connect, TLS handshake, each send and receive).
WRITE_TIMEOUT_S = 20
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
# How much of a response body a diagnostic quotes (WriteReply bounds it again).
_SNIPPET = 300


def _returned_object(stdout: str) -> dict | None:
    try:
        obj = json.loads(stdout)
    except ValueError:
        return None
    return obj if type(obj) is dict else None


def _response_text(exchange: PatchExchange) -> str:
    """A short, safe snippet of the response for a diagnostic: the Kubernetes Status message
    when the body is a complete strict Status object, else the first bytes as text."""
    if not exchange.body_truncated:
        try:
            obj = strict_json_object(exchange.body_prefix.decode("utf-8"))
        except (UnicodeDecodeError, MalformedJSON):
            obj = None
        if obj is not None and type(obj.get("message")) is str:
            return obj["message"][:_SNIPPET]
    text = exchange.body_prefix[:_SNIPPET].decode("utf-8", errors="replace").strip()
    return text + (" [truncated]" if exchange.body_truncated else "")


def _complete_object(exchange: PatchExchange) -> dict | None:
    """The response body as one complete, strictly parsed Kubernetes object, or None."""
    if exchange.body_truncated or exchange.error is not None:
        return None
    try:
        obj = strict_json_object(exchange.body_prefix.decode("utf-8"))
    except (UnicodeDecodeError, MalformedJSON):
        return None
    if type(obj.get("kind")) is not str or type(obj.get("metadata")) is not dict:
        return None
    return obj


def classify_exchange(exchange: PatchExchange) -> WriteReply:
    """What one direct PATCH did, as one of the five WriteClass values (D§11.3; M8B-2 STOP
    resolution). Total: a reading bug is AMBIGUOUS (the write may have landed)."""
    try:
        return _classify(exchange)
    except Exception as exc:
        return WriteReply(WriteClass.AMBIGUOUS, diagnostic=f"unreadable outcome: {exc}")


def _classify(exchange: PatchExchange) -> WriteReply:
    if exchange.send_state is SendState.PRE_SEND:  # proven unsent: nothing reached the server
        return WriteReply(
            WriteClass.NOT_ATTEMPTED, diagnostic=f"not sent: {exchange.error or 'no request'}"
        )
    status = exchange.status
    if status is None:  # sent, or partly sent, and no response: it may have landed
        how = "timed out" if exchange.timed_out else "lost"
        return WriteReply(
            WriteClass.AMBIGUOUS,
            diagnostic=f"the PATCH {how} after sending began: {exchange.error}",
        )
    if 200 <= status < 300:
        obj = _complete_object(exchange)
        if obj is not None:
            return WriteReply(WriteClass.SUCCESS, obj, f"HTTP {status}")
        why = (
            "the response body was over the cap"
            if exchange.body_truncated
            else f"the response is not one complete strict object ({exchange.error or 'body'})"
        )
        return WriteReply(WriteClass.AMBIGUOUS, diagnostic=f"HTTP {status}, but {why}")
    detail = f"HTTP {status}: {_response_text(exchange)}"
    if 300 <= status < 400:  # never followed, and the PATCH was sent: it may have landed
        return WriteReply(WriteClass.AMBIGUOUS, diagnostic=f"redirect not followed; {detail}")
    if status == 422:
        if _invalid_status(exchange):
            return WriteReply(WriteClass.INVALID_ACTION, diagnostic=detail)
        return WriteReply(
            WriteClass.DEFINITE_FAILURE, diagnostic=f"{detail} (not a complete Invalid Status)"
        )
    if 400 <= status < 500:
        return WriteReply(WriteClass.DEFINITE_FAILURE, diagnostic=detail)
    return WriteReply(WriteClass.AMBIGUOUS, diagnostic=detail)  # 5xx, or anything else


def _invalid_status(exchange: PatchExchange) -> bool:
    """A 422 is INVALID_ACTION only with proof: a complete, strictly parsed Status object
    whose reason is "Invalid"."""
    if exchange.body_truncated or exchange.error is not None:
        return False
    try:
        obj = strict_json_object(exchange.body_prefix.decode("utf-8"))
    except (UnicodeDecodeError, MalformedJSON):
        return False
    return obj.get("kind") == "Status" and obj.get("reason") == "Invalid"


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
        connect: Callable = https_connection,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(cfg, ClusterConfig):
            raise TypeError("the writer needs the loaded ClusterConfig")
        if not isinstance(audit, ExecutorAudit):
            raise TypeError(f"{type(audit).__name__} is not an ExecutorAudit")
        self.cfg = cfg
        self.audit = audit
        self._runner = runner
        self._connect = connect
        self._now = now
        self._client: ExecutorPatchClient | None = None
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
        if path is not None and self._client is None:
            # Loads and checks the credential, the pinned CA and the API server's identity.
            self._client = ExecutorPatchClient(
                self.cfg, runner=self._runner, connect=self._connect, now=self._now
            )  # only a verified client is kept
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
        if self._client is None:
            return self._after(
                request,
                WriteReply(
                    WriteClass.NOT_ATTEMPTED,
                    diagnostic="the executor credential was unavailable at preflight, so its"
                    " identity was never verified; not sent",
                ),
            )
        try:
            exchange = self._client.patch(
                kind=request.kind,
                name=request.name,
                namespace=request.namespace,
                patch_type=request.patch_type.value,
                body_json=request.body_json,
                timeout_s=WRITE_TIMEOUT_S,
            )
        except GuardError:
            raise  # the request guard refused before anything was sent: a harness bug
        except Exception as exc:  # unexpected: the PATCH may have been sent
            reply = WriteReply(WriteClass.AMBIGUOUS, diagnostic=f"{type(exc).__name__}: {exc}")
        else:
            reply = classify_exchange(exchange)  # one PATCH, never retried
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
