"""The executor's one capability: a direct HTTPS PATCH to the Kubernetes API server (decision
0025 D§14; M8B-2 STOP-resolution amendment).

Why it exists: kubectl v1.37.2 GETs the object before `kubectl patch`, and safeops-executor
may not read (patch-only RBAC). So the executor's write skips kubectl and sends the PATCH
itself. This module is executor-write-only: no GET, no LIST, no generic Kubernetes client.
The executor's reads stay on `Kubectl(cfg, Phase.READER)` (safeops.executor.kubernetes).

**Construction** (`ExecutorPatchClient(cfg)`) reads the configured executor credential, and
only it, and refuses loudly before any network use:
- missing or expired credential: CredentialUnavailable (the writer reports NOT_ATTEMPTED);
- a kubeconfig without a usable context, a token that isn't safeops-executor's, a missing,
  empty or unusable `certificate-authority-data`, `insecure-skip-tls-verify`, a
  `tls-server-name` or any other cluster or user key outside the bootstrap contract (which
  writes exactly `server` + `certificate-authority-data`, and `token`): CredentialError;
- a server that isn't the loopback API server Docker publishes for this cluster's kind node:
  GuardError (cluster.verify_api_server, shared with kubectl).

**The request** (`patch(...)`), checked before anything is sent (GuardError): the kind is
Deployment, Service or ConfigMap; the name is a DNS label; the namespace passes
`check_object` (directly, never `_check_objects`, which would ask the API server for its
kinds); the patch type is json, merge or strategic with a body of the right JSON shape; the
timeout is a finite positive number. The API path is fixed per kind (no discovery):
`/apis/apps/v1/namespaces/{ns}/deployments/{name}`, `/api/v1/namespaces/{ns}/services/{name}`,
`/api/v1/namespaces/{ns}/configmaps/{name}`.

**Transport rules**, all structural:
- `http.client.HTTPSConnection` to the verified authority only. The caller can't name a host,
  port, URL, header, token, CA or TLS setting: the signature has no such parameter.
- TLS: an `SSLContext` loaded only from the kubeconfig-pinned CA (never system trust),
  CERT_REQUIRED, hostname checking on.
- No proxy: HTTPSConnection consults no environment or system proxy, and `set_tunnel` is
  never called. No redirect: http.client follows none, and a 3xx is returned as it came.
- The bearer token goes only into the Authorization header; never into a URL, diagnostic,
  repr or exception text (an error message is scrubbed of it).
- An explicit send boundary: PRE_SEND until the request starts going out (connect, the TLS
  handshake and buffering the request line and headers send nothing), SEND_STARTED from
  `endheaders(body)` on. A failure in PRE_SEND proves nothing was sent; after SEND_STARTED
  the server may have received it.
- The response body is read with `read_bounded_bytes`, at most MAX_EXECUTOR_HTTP_BODY_BYTES
  + 1 bytes, never sized from Content-Length.

`patch` never raises for a transport outcome: it returns a PatchExchange, neutral evidence
the executor's adapter classifies. It holds no token, header, CA or kubeconfig content.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import re
import ssl
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit

from safeops.bounded import read_bounded_bytes
from safeops.cluster import (
    EXECUTOR_PATCH_TYPES,
    ClusterConfig,
    CredentialError,
    GuardError,
    Phase,
    Runner,
    check_object,
    executor_credential,
    verify_api_server,
)

MAX_EXECUTOR_HTTP_BODY_BYTES = 1_048_576
EXECUTOR_SERVICE_ACCOUNT = "safeops-executor"

# The fixed API path of each kind the executor patches: (group-version prefix, plural).
API_PATHS = {
    "Deployment": ("/apis/apps/v1", "deployments"),
    "Service": ("/api/v1", "services"),
    "ConfigMap": ("/api/v1", "configmaps"),
}
PATCH_MEDIA_TYPES = {
    "strategic": "application/strategic-merge-patch+json",
    "merge": "application/merge-patch+json",
    "json": "application/json-patch+json",
}
assert frozenset(PATCH_MEDIA_TYPES) == EXECUTOR_PATCH_TYPES

# Exactly what bootstrap writes (bootstrap._issue_credential copies kind's cluster entry).
_CLUSTER_KEYS = frozenset({"server", "certificate-authority-data"})
_USER_KEYS = frozenset({"token"})
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_TOKEN = re.compile(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_MAX_ERROR = 512


class SendState(Enum):
    PRE_SEND = "pre_send"  # nothing has been sent
    SEND_STARTED = "send_started"  # the request may have reached the server


@dataclass(frozen=True)
class PatchExchange:
    """What one PATCH did, uninterpreted: bounded, credential-free evidence."""

    send_state: SendState
    status: int | None = None  # the HTTP status, if a response arrived
    body_prefix: bytes = field(default=b"", repr=False)  # at most the body cap
    body_truncated: bool = False  # more than MAX_EXECUTOR_HTTP_BODY_BYTES was there
    response_received: bool = False
    timed_out: bool = False
    error: str | None = None  # "<ExceptionType>: <message>", token-free, bounded

    def __post_init__(self) -> None:
        if type(self.send_state) is not SendState:
            raise TypeError("send_state must be a SendState")
        if self.send_state is SendState.PRE_SEND and (
            self.status is not None or self.response_received
        ):
            raise ValueError("nothing was sent, so no response can have arrived")
        if self.response_received != (self.status is not None):
            raise ValueError("a status is present exactly when a response arrived")
        if len(self.body_prefix) > MAX_EXECUTOR_HTTP_BODY_BYTES:
            raise ValueError("the body prefix is over the cap")


def https_connection(
    host: str, port: int, *, context: ssl.SSLContext, timeout: float
) -> http.client.HTTPSConnection:
    """The production connection: direct to host:port, the pinned context, the timeout."""
    return http.client.HTTPSConnection(host, port, context=context, timeout=timeout)


def _authority(server: object) -> tuple[str, int]:
    """(host, port) of an `https://host:port` server URL, or CredentialError."""
    if type(server) is not str:
        raise CredentialError("the executor kubeconfig's server is not a URL")
    parts = urlsplit(server)
    try:
        port = parts.port
    except ValueError:
        port = None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or port is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise CredentialError(
            "the executor kubeconfig's server must be exactly https://host:port"
            f" (got scheme {parts.scheme!r})"
        )
    return parts.hostname, port


def _pinned_context(cluster: dict, name: str) -> ssl.SSLContext:
    """A TLS context that trusts only the kubeconfig's CA. Never system trust."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    ca_data = cluster.get("certificate-authority-data")
    if type(ca_data) is not str or not ca_data.strip():
        raise CredentialError(
            f"{name} has no certificate-authority-data; the executor never falls back to"
            " system trust"
        )
    try:
        pem = base64.b64decode(ca_data, validate=True).decode("ascii")
        context.load_verify_locations(cadata=pem)
    except (ValueError, UnicodeDecodeError, ssl.SSLError):
        raise CredentialError(f"{name} has an unusable certificate-authority-data") from None
    if not context.get_ca_certs():
        raise CredentialError(f"{name}'s certificate-authority-data holds no CA certificate")
    return context


def _token_subject(token: str) -> object:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("sub")


class ExecutorPatchClient:
    """safeops-executor's direct PATCH (module docstring). Built only from the configured
    executor credential; every refusal happens here, before any network use."""

    def __init__(
        self,
        cfg: ClusterConfig,
        *,
        runner: Runner = subprocess.run,
        connect: Callable[..., http.client.HTTPSConnection] = https_connection,
        now: Callable[[], float] = time.time,
    ) -> None:
        import yaml

        if not isinstance(cfg, ClusterConfig):
            raise TypeError("the executor's PATCH needs the loaded ClusterConfig")
        path = executor_credential(cfg, now)  # missing or expired: CredentialUnavailable
        name = path.name
        identity = f"system:serviceaccount:{cfg.system_namespace}:{EXECUTOR_SERVICE_ACCOUNT}"
        try:
            kc = yaml.safe_load(path.read_text())
            ctx = next(c["context"] for c in kc["contexts"] if c["name"] == cfg.kube_context)
            cluster = next(c["cluster"] for c in kc["clusters"] if c["name"] == ctx["cluster"])
            user_name = ctx["user"]
            user = next(u["user"] for u in kc["users"] if u["name"] == user_name)
            if type(cluster) is not dict or type(user) is not dict:
                raise TypeError
        except Exception:
            # `from None`: a YAML error can quote the file, and the file holds the token.
            raise CredentialError(f"{name} has no usable {cfg.kube_context!r} context") from None
        if cluster.get("insecure-skip-tls-verify"):
            raise CredentialError(f"{name} sets insecure-skip-tls-verify; refusing it")
        if "tls-server-name" in cluster:
            raise CredentialError(f"{name} sets tls-server-name, which bootstrap never writes")
        extra = sorted(set(cluster) - _CLUSTER_KEYS) + sorted(set(user) - _USER_KEYS)
        if extra:
            raise CredentialError(f"{name} has keys outside the bootstrap contract: {extra}")
        token = user.get("token")
        if user_name != EXECUTOR_SERVICE_ACCOUNT or type(token) is not str:
            raise CredentialError(f"{name} does not hold the {EXECUTOR_SERVICE_ACCOUNT} identity")
        try:
            subject = _token_subject(token) if _TOKEN.fullmatch(token) else None
        except Exception:
            subject = None
        if subject != identity:
            raise CredentialError(f"{name}'s token is not {identity}'s")
        context = _pinned_context(cluster, name)
        server = cluster.get("server")
        host, port = _authority(server)
        verify_api_server(cfg, server, runner)  # another cluster's API server: GuardError
        self.cfg = cfg
        self.identity = identity
        self._host, self._port = host, port
        self._context = context
        self._connect = connect
        self.__token = token

    def __repr__(self) -> str:
        return f"ExecutorPatchClient(authority={self._host}:{self._port})"

    @property
    def authority(self) -> tuple[str, int]:
        return self._host, self._port

    def _safe(self, exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"
        text = text.replace(self.__token, "<redacted>")
        return text[:_MAX_ERROR]

    def patch(
        self,
        *,
        kind: str,
        name: str,
        namespace: str,
        patch_type: str,
        body_json: str,
        timeout_s: float,
    ) -> PatchExchange:
        """One PATCH of one object (module docstring). GuardError before anything is sent
        if the request is out of scope; otherwise a PatchExchange, never a raise."""
        target = API_PATHS.get(kind)
        if target is None:
            raise GuardError(f"the executor may not patch kind {kind!r}")
        if type(name) is not str or not _NAME.fullmatch(name):
            raise GuardError(f"invalid object name {name!r}")
        if type(namespace) is not str or not _NAME.fullmatch(namespace):
            raise GuardError(f"invalid namespace {namespace!r}")
        media_type = PATCH_MEDIA_TYPES.get(patch_type)
        if media_type is None:
            raise GuardError(f"patch type {patch_type!r} is not json, merge or strategic")
        try:
            parsed = json.loads(body_json)
        except (TypeError, ValueError):
            raise GuardError("the patch body is not JSON") from None
        if type(parsed) is not (list if patch_type == "json" else dict):
            raise GuardError(f"a {patch_type} patch body has the wrong JSON shape")
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise GuardError(f"the PATCH needs a finite positive timeout, not {timeout_s!r}")
        check_object(self.cfg, Phase.EXECUTOR, kind=kind, name=name, namespace=namespace)
        prefix, plural = target
        path = f"{prefix}/namespaces/{namespace}/{plural}/{name}"
        payload = body_json.encode("utf-8")

        state = SendState.PRE_SEND
        status: int | None = None
        conn = None
        try:
            conn = self._connect(self._host, self._port, context=self._context, timeout=timeout_s)
            conn.connect()  # TCP and the TLS handshake: nothing of the request is sent yet
            conn.putrequest("PATCH", path, skip_accept_encoding=True)
            conn.putheader("Authorization", f"Bearer {self.__token}")
            conn.putheader("Content-Type", media_type)
            conn.putheader("Accept", "application/json")
            conn.putheader("Content-Length", str(len(payload)))
            conn.putheader("User-Agent", EXECUTOR_SERVICE_ACCOUNT)
            state = SendState.SEND_STARTED
            conn.endheaders(payload)
            response = conn.getresponse()
            try:
                if type(response.status) is not int:
                    raise http.client.HTTPException("the response has no integer status")
                status = response.status
                body, truncated = read_bounded_bytes(response, MAX_EXECUTOR_HTTP_BODY_BYTES)
            finally:
                response.close()
        except Exception as exc:
            return PatchExchange(
                state,
                status=status,
                response_received=status is not None,
                timed_out=isinstance(exc, TimeoutError),
                error=self._safe(exc),
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return PatchExchange(
            SendState.SEND_STARTED,
            status=status,
            body_prefix=body,
            body_truncated=truncated,
            response_received=True,
        )
