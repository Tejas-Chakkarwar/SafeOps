"""Offline doubles for the M8B-2 concrete transports (decision 0025, M8B-2 amendment and STOP
resolution).

`ProcessCluster` stands in for everything the transports reach, over a FakeCluster:
- the processes they run (it is a `subprocess.run` double): `docker port`, the API-server
  identity check, and the reader's `kubectl get`, which answers with the object or nothing
  (--ignore-not-found);
- the executor's direct HTTPS PATCH (`ProcessCluster.connect` is the writer's connection
  factory): a PATCH is applied the way the API server would and answered with the object, or
  with the scripted outcome. Every request is recorded (method, path, headers, body), so a
  test can prove what was sent, and in what order relative to the audit file.

`credentials(cfg, ...)` writes a scratch executor (and reader) kubeconfig in exactly the shape
bootstrap writes: `server` + `certificate-authority-data` (a real, throwaway, public test CA
certificate; its key was discarded), and a token that is a JWT naming safeops-executor
(unsigned: nothing here verifies signatures, the API server does).
"""

from __future__ import annotations

import base64
import json
import ssl
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from safeops.executor.transport import PatchType
from tests.conftest import scratch_cluster_config
from tests.m8_executor_fakes import FakeCluster, seed_objects

PORT = 43210
SERVER = f"https://127.0.0.1:{PORT}"
FAR = 2**40  # an expiry far in the future
PLURAL = {"deployments.apps": "deployments", "services": "services", "configmaps": "configmaps"}
KIND = {"deployments.apps": "Deployment", "services": "Service", "configmaps": "ConfigMap"}
PATH_KIND = {"deployments": "Deployment", "services": "Service", "configmaps": "ConfigMap"}
EXECUTOR_SUB = "system:serviceaccount:safeops-system:safeops-executor"

TEST_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBmjCCAUGgAwIBAgIUeCx3eBR6FsCV6GZeYP9tPUNtG28wCgYIKoZIzj0EAwIw
IjEgMB4GA1UEAwwXc2FmZW9wcy1vZmZsaW5lLXRlc3QtY2EwIBcNMjYxMTI2MjAw
MDA5WhgPMjEyNjExMDIyMDAwMDlaMCIxIDAeBgNVBAMMF3NhZmVvcHMtb2ZmbGlu
ZS10ZXN0LWNhMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEraSdIiS91/48Vud+
azmPAxj7noQRgA6sij63Bf07o4Tvb30jgLoIUXBQBSzRs4OwZFekH5UXfKSXNOtc
aJSfyaNTMFEwHQYDVR0OBBYEFGrlRr3FteEj1I3hIZluTo8xnkfYMB8GA1UdIwQY
MBaAFGrlRr3FteEj1I3hIZluTo8xnkfYMA8GA1UdEwEB/wQFMAMBAf8wCgYIKoZI
zj0EAwIDRwAwRAIgA7R8J1x5Ff5dpDhkHhPGRI7z7KcjLlu4lGKylbhzRPkCIBaw
FqpmLh1Wt4mlT2o5ESRWdNkYgYTp8JWYs0lflZ4I
-----END CERTIFICATE-----
"""
TEST_CA_DATA = base64.b64encode(TEST_CA_PEM.encode()).decode()


def _b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def fake_token(sub: str = EXECUTOR_SUB) -> str:
    """A JWT-shaped token (header.payload.signature) naming `sub`. Not a real credential."""
    header = _b64url({"alg": "RS256", "kid": "offline-test"})
    return f"{header}.{_b64url({'sub': sub, 'iat': 1, 'exp': FAR})}.c2lnbmF0dXJl"


TOKEN = fake_token()


def scratch_cfg(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return scratch_cluster_config(state)


def kubeconfig(
    server: str = SERVER,
    *,
    user: str = "safeops-executor",
    token: str | None = None,
    cluster_extra: dict | None = None,
    ca: str | None = TEST_CA_DATA,
    user_extra: dict | None = None,
) -> str:
    cluster = {"server": server}
    if ca is not None:
        cluster["certificate-authority-data"] = ca
    cluster.update(cluster_extra or {})
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "kind-safeops", "cluster": cluster}],
            "users": [
                {
                    "name": user,
                    "user": {"token": TOKEN if token is None else token, **(user_extra or {})},
                }
            ],
            "contexts": [
                {"name": "kind-safeops", "context": {"cluster": "kind-safeops", "user": user}}
            ],
        }
    )


def credentials(
    cfg, *, executor: str = "valid", reader: bool = True, server: str = SERVER, text=None
):
    """executor: valid | expired | missing | corrupt (exists, but no usable context).
    `text` replaces the executor kubeconfig's content (a crafted variant)."""
    if executor != "missing":
        if text is None:
            text = "this is not a kubeconfig" if executor == "corrupt" else kubeconfig(server)
        cfg.executor_kubeconfig_path.write_text(text)
        expires = 1 if executor == "expired" else FAR
        cfg.executor_token_meta_path.write_text(json.dumps({"expires_at": expires}))
    if reader:
        cfg.reader_kubeconfig_path.write_text(
            kubeconfig(server, user="safeops-reader", token=fake_token("reader"))
        )
        cfg.reader_token_meta_path.write_text(json.dumps({"expires_at": FAR}))
    return cfg


@dataclass
class _Patch:
    kind: str
    name: str
    patch_type: PatchType
    body_json: str

    def body(self):
        return json.loads(self.body_json)


@dataclass
class HttpRequest:
    """One request the writer sent through a FakeConnection."""

    host: str
    port: int
    method: str = ""
    path: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes | None = None
    skip_accept_encoding: bool = False

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]


MEDIA = {
    "application/strategic-merge-patch+json": PatchType.STRATEGIC,
    "application/merge-patch+json": PatchType.MERGE,
    "application/json-patch+json": PatchType.JSON,
}


def status_body(code: int, reason: str, message: str) -> bytes:
    return json.dumps(
        {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": message,
            "reason": reason,
            "code": code,
        }
    ).encode()


class FakeResponse:
    """An http.client.HTTPResponse double. `read` refuses an unbounded call."""

    def __init__(self, status: int, body: bytes, *, fail_read: Exception | None = None):
        self.status = status
        self._body = body
        self._fail = fail_read
        self.read_sizes: list[int | None] = []
        self.closed = False

    def read(self, amt=None):
        self.read_sizes.append(amt)
        if amt is None or amt < 0:
            raise AssertionError("an unbounded response read")
        if self._fail is not None:
            raise self._fail
        out, self._body = self._body[:amt], self._body[amt:]
        return out

    def close(self):
        self.closed = True


class FakeConnection:
    """An http.client.HTTPSConnection double over ProcessCluster."""

    def __init__(self, api: ProcessCluster, host, port, context, timeout):
        self.api, self.context, self.timeout = api, context, timeout
        self.request = HttpRequest(host, port)
        self.connected = self.closed = False
        self.response: FakeResponse | None = None

    def connect(self):
        how = self.api.peek()
        if how in ("refused", "tls"):
            self.api.script.pop(0)
            raise (
                ConnectionRefusedError(61, "Connection refused")
                if how == "refused"
                else ssl.SSLCertVerificationError("certificate verify failed")
            )
        self.connected = True

    def set_tunnel(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("set_tunnel must never be called")

    def putrequest(self, method, url, skip_host=False, skip_accept_encoding=False):
        assert self.connected, "putrequest before connect"
        self.request.method, self.request.path = method, url
        self.request.skip_accept_encoding = skip_accept_encoding

    def putheader(self, name, *values):
        self.request.headers.append((name, ", ".join(str(v) for v in values)))

    def endheaders(self, message_body=None):
        self.request.body = message_body
        self.api.requests.append(self.request)
        if self.api.on_patch is not None:
            self.api.on_patch(self.request)
        self.response = self.api.serve(self.request)

    def getresponse(self):
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


class ProcessCluster:
    """A subprocess.run double and the writer's connection factory (module docstring).

    PATCH outcomes (one per PATCH, default "success"):
      success    applied; 200 with the object
      invalid    not applied; 422 Status reason Invalid
      forbidden  not applied; 403
      notfound   not applied; 404
      conflict   not applied; 409
      throttled  not applied; 429
      server     applied, then 500
      redirect   not applied; 307 with a Location
      timeout    applied, then the response timed out
      drop       sending broke (ConnectionResetError in endheaders); not applied
      refused    connection refused at connect: nothing sent
      tls        certificate verification failed at connect: nothing sent
      raise      applied, then getresponse raised something unexpected
      huge       applied; 200 with a body over the cap
    Any (status, body bytes) tuple or FakeResponse is served as is, and any exception is
    raised from getresponse, without applying.
    """

    def __init__(self, cluster: FakeCluster | None = None, script=(), *, docker_port=PORT):
        self.cluster = cluster or FakeCluster(seed_objects())
        self.script = list(script)
        self.docker_port = docker_port
        self.calls: list[list[str]] = []
        self.requests: list[HttpRequest] = []
        self.connections: list[FakeConnection] = []
        self.on_patch = None  # called with the request just before the PATCH is "served"

    @property
    def patches(self) -> list[HttpRequest]:
        return [r for r in self.requests if r.method == "PATCH"]

    @property
    def gets(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "kubectl" and "get" in c]

    def peek(self):
        return self.script[0] if self.script else "success"

    # --- the writer's connection factory ------------------------------------------------

    def connect(self, host, port, *, context, timeout):
        conn = FakeConnection(self, host, port, context, timeout)
        self.connections.append(conn)
        return conn

    def serve(self, request: HttpRequest):
        how = self.script.pop(0) if self.script else "success"
        if isinstance(how, tuple):
            return FakeResponse(*how)
        if isinstance(how, (FakeResponse, BaseException)):  # served (or raised) as given
            return how
        if how == "drop":
            raise ConnectionResetError(54, "Connection reset by peer")
        parts = request.path.split("/")  # /api(s)/.../namespaces/<ns>/<plural>/<name>
        kind, name = PATH_KIND[parts[-2]], parts[-1]
        (media,) = request.header("Content-Type")
        req = _Patch(kind, name, MEDIA[media], request.body.decode())
        if how in ("success", "server", "timeout", "raise", "huge"):
            self.cluster.apply(req)
        if how == "timeout":
            return TimeoutError("The read operation timed out")
        if how == "raise":
            return RuntimeError("the connection broke after the request")
        if how == "success":
            return FakeResponse(200, json.dumps(self.cluster.get(kind, name)).encode())
        if how == "huge":
            obj = dict(self.cluster.get(kind, name), padding="x" * (2 * 1024 * 1024))
            return FakeResponse(200, json.dumps(obj).encode())
        if how == "invalid":
            message = f'{kind} "{name}" is invalid: spec.template: Invalid value'
            return FakeResponse(422, status_body(422, "Invalid", message))
        if how == "redirect":
            return FakeResponse(307, b"")
        code, reason = {
            "forbidden": (403, "Forbidden"),
            "notfound": (404, "NotFound"),
            "conflict": (409, "Conflict"),
            "throttled": (429, "TooManyRequests"),
            "server": (500, "InternalError"),
        }[how]
        return FakeResponse(code, status_body(code, reason, f"{reason} (offline test)"))

    # --- the processes ------------------------------------------------------------------

    def __call__(self, args, input=None, capture_output=True, text=True, timeout=None, check=False):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["docker", "port"]:
            return subprocess.CompletedProcess(args, 0, f"127.0.0.1:{self.docker_port}\n", "")
        assert args[0] == "kubectl" and args[1] == "--kubeconfig" and args[3] == "--context"
        if args[5] == "get":
            return self._get(args)
        raise AssertionError(f"unexpected kubectl command {args[5:]}")

    def _get(self, args):
        resource = args[6]
        if resource == "replicasets.apps":
            items = [o for (k, _), o in self.cluster.objects.items() if k == "ReplicaSet"]
            out = {"apiVersion": "v1", "kind": "List", "items": items, "metadata": {}}
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        obj = self.cluster.get(KIND[resource], args[7])
        return subprocess.CompletedProcess(args, 0, "" if obj is None else json.dumps(obj), "")
