"""Milestone 8B-2, offline: the executor's direct HTTPS PATCH (decision 0025, M8B-2 STOP
resolution). kubectl patch GETs the object first and safeops-executor may not read, so the
writer sends the PATCH itself, to the verified API server, with the pinned CA.

Covered here: the credential and TLS material, the verified authority, the exact request
(path, method, media type, headers), the primitive's signature, the absence of proxy and
redirect machinery, the PRE_SEND -> SEND_STARTED boundary, the bounded response body, the
neutral hardened JSON parser, and the five-class classification. Nothing here touches a
network: the connection is a FakeConnection over ProcessCluster.
"""

from __future__ import annotations

import ast
import http.client
import inspect
import json
import math
import ssl
from pathlib import Path

import pytest

from safeops.bounded import read_bounded_bytes
from safeops.cluster import (
    REPO_ROOT,
    CredentialError,
    CredentialUnavailable,
    GuardError,
    Kubectl,
)
from safeops.domain import MAX_DISCARD_ERROR
from safeops.enums import ActionType
from safeops.executor.kubernetes import (
    KubernetesReaderTransport,
    KubernetesWriterTransport,
    classify_exchange,
)
from safeops.executor.transport import Check, PatchType, WriteClass, prepared
from safeops.jsonutil import MalformedJSON, strict_json_object
from safeops.kube_patch import (
    API_PATHS,
    MAX_EXECUTOR_HTTP_BODY_BYTES,
    PATCH_MEDIA_TYPES,
    ExecutorPatchClient,
    PatchExchange,
    SendState,
    https_connection,
)
from tests.m8_executor_fakes import RecordingAudit
from tests.m8b2_fakes import (
    PORT,
    SERVER,
    TEST_CA_DATA,
    TOKEN,
    FakeResponse,
    ProcessCluster,
    credentials,
    fake_token,
    kubeconfig,
    scratch_cfg,
    status_body,
)

W = WriteClass
MAX = MAX_EXECUTOR_HTTP_BODY_BYTES
OBJ = {"kind": "Deployment", "metadata": {"name": "orders", "namespace": "testbed"}}
KUBE_PATCH = REPO_ROOT / "safeops" / "kube_patch.py"
KUBERNETES = REPO_ROOT / "safeops" / "executor" / "kubernetes.py"
ARGS = dict(
    kind="Deployment",
    name="orders",
    namespace="testbed",
    patch_type="merge",
    body_json='{"spec":{"replicas":4}}',
    timeout_s=20,
)


def _never(*a, **k):
    raise AssertionError("nothing may run or connect")


def _client(tmp_path, *, script=(), text=None, docker_port=PORT, connect=None):
    cfg = credentials(scratch_cfg(tmp_path), text=text)
    api = ProcessCluster(script=script, docker_port=docker_port)
    client = ExecutorPatchClient(cfg, runner=api, connect=connect or api.connect)
    return client, api


def _patch(tmp_path, script=(), **overrides):
    client, api = _client(tmp_path, script=script)
    return client.patch(**{**ARGS, **overrides}), api


def _exchange(status, body=b"", *, truncated=False, error=None, timed_out=False):
    return PatchExchange(
        SendState.SEND_STARTED,
        status=status,
        body_prefix=body,
        body_truncated=truncated,
        response_received=status is not None,
        timed_out=timed_out,
        error=error,
    )


def _source_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


# --- construction: credential, CA, identity, authority -----------------------------------------


def test_a_valid_credential_builds_a_client_bound_to_the_verified_authority(tmp_path) -> None:
    client, api = _client(tmp_path)
    assert client.authority == ("127.0.0.1", PORT)
    assert client.identity == "system:serviceaccount:safeops-system:safeops-executor"
    assert [c[:2] for c in api.calls] == [["docker", "port"]]  # the identity check, no kubectl
    assert api.requests == []  # nothing sent at construction
    assert TOKEN not in repr(client) and TOKEN not in str(vars(client).get("_host"))


@pytest.mark.parametrize("executor", ["missing", "expired"])
def test_a_missing_or_expired_credential_is_unavailable_with_zero_network(tmp_path, executor):
    cfg = credentials(scratch_cfg(tmp_path), executor=executor)
    with pytest.raises(CredentialUnavailable):
        ExecutorPatchClient(cfg, runner=_never, connect=_never)


def test_a_corrupt_kubeconfig_is_a_loud_credential_error(tmp_path) -> None:
    cfg = credentials(scratch_cfg(tmp_path), executor="corrupt")
    with pytest.raises(CredentialError, match="no usable") as exc:
        ExecutorPatchClient(cfg, runner=_never, connect=_never)
    assert not isinstance(exc.value, CredentialUnavailable)


def test_a_yaml_error_never_chains_the_file_into_the_exception(tmp_path) -> None:
    text = f"users: [{{token: {TOKEN}\n  : : broken"
    cfg = credentials(scratch_cfg(tmp_path), text=text)
    with pytest.raises(CredentialError) as exc:
        ExecutorPatchClient(cfg, runner=_never, connect=_never)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    "ca, match",
    [
        (None, "never falls back to system trust"),
        ("", "never falls back to system trust"),
        ("   ", "never falls back to system trust"),
        ("!!!not base64!!!", "unusable"),
        ("bm90IGEgY2VydGlmaWNhdGU=", "unusable"),  # base64 of "not a certificate"
    ],
    ids=["absent", "empty", "blank", "not-base64", "not-a-cert"],
)
def test_the_kubeconfig_ca_is_mandatory_and_must_be_usable(tmp_path, ca, match) -> None:
    cfg = credentials(scratch_cfg(tmp_path), text=kubeconfig(ca=ca))
    with pytest.raises(CredentialError, match=match) as exc:
        ExecutorPatchClient(cfg, runner=_never, connect=_never)  # zero network
    assert not isinstance(exc.value, CredentialUnavailable)


@pytest.mark.parametrize(
    "extra, match",
    [
        ({"insecure-skip-tls-verify": True}, "insecure-skip-tls-verify"),
        ({"tls-server-name": "kubernetes"}, "tls-server-name"),
        ({"certificate-authority": "/etc/ca.pem"}, "outside the bootstrap contract"),
        ({"proxy-url": "http://proxy:3128"}, "outside the bootstrap contract"),
    ],
)
def test_tls_overrides_and_unknown_cluster_keys_are_refused(tmp_path, extra, match) -> None:
    cfg = credentials(scratch_cfg(tmp_path), text=kubeconfig(cluster_extra=extra))
    with pytest.raises(CredentialError, match=match):
        ExecutorPatchClient(cfg, runner=_never, connect=_never)


@pytest.mark.parametrize(
    "text",
    [
        kubeconfig(user="safeops-reader"),
        kubeconfig(token=fake_token("system:serviceaccount:safeops-system:safeops-reader")),
        kubeconfig(token="not-a-jwt"),
        kubeconfig(token="a.b\nInjected: header.c"),
        kubeconfig(user_extra={"client-certificate-data": "eA=="}),
    ],
    ids=["reader-user", "reader-token", "not-a-jwt", "header-injection", "client-cert"],
)
def test_a_credential_that_is_not_the_executor_s_is_loud(tmp_path, text) -> None:
    cfg = credentials(scratch_cfg(tmp_path), text=text)
    with pytest.raises(CredentialError) as exc:
        ExecutorPatchClient(cfg, runner=_never, connect=_never)
    assert not isinstance(exc.value, CredentialUnavailable)
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    "server",
    [
        f"http://127.0.0.1:{PORT}",
        "https://127.0.0.1",
        f"https://127.0.0.1:{PORT}/prefix",
        f"https://user:pw@127.0.0.1:{PORT}",
        f"https://127.0.0.1:{PORT}?x=1",
    ],
    ids=["http", "no-port", "path", "userinfo", "query"],
)
def test_the_server_must_be_exactly_https_host_port(tmp_path, server) -> None:
    cfg = credentials(scratch_cfg(tmp_path), text=kubeconfig(server))
    with pytest.raises(CredentialError, match="https://host:port"):
        ExecutorPatchClient(cfg, runner=_never, connect=_never)


def test_another_cluster_s_api_server_is_a_guard_error_before_any_connection(tmp_path) -> None:
    with pytest.raises(GuardError, match="not the API server"):
        _client(tmp_path, docker_port=55555, connect=_never)


def test_the_tls_context_trusts_only_the_pinned_ca_and_checks_the_hostname(tmp_path) -> None:
    client, api = _client(tmp_path)
    client.patch(**ARGS)
    context = api.connections[0].context
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname is True
    cas = context.get_ca_certs()
    assert len(cas) == 1  # the kubeconfig's CA, and no system trust store
    assert dict(x[0] for x in cas[0]["subject"])["commonName"] == "safeops-offline-test-ca"


# --- the request ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, name, path",
    [
        ("Deployment", "orders", "/apis/apps/v1/namespaces/testbed/deployments/orders"),
        ("Service", "orders-svc", "/api/v1/namespaces/testbed/services/orders-svc"),
        ("ConfigMap", "db-pool-config", "/api/v1/namespaces/testbed/configmaps/db-pool-config"),
    ],
)
def test_each_kind_is_one_patch_to_its_exact_path(tmp_path, kind, name, path) -> None:
    body = '{"metadata":{"labels":{"x":"y"}}}'
    exchange, api = _patch(tmp_path, kind=kind, name=name, body_json=body)
    assert exchange.status == 200
    assert [(r.method, r.path) for r in api.requests] == [("PATCH", path)]  # no GET, one PATCH
    assert api.gets == [] and not any("api-resources" in a for c in api.calls for a in c)


@pytest.mark.parametrize(
    "patch_type, media, body",
    [
        ("strategic", "application/strategic-merge-patch+json", '{"spec":{"replicas":4}}'),
        ("merge", "application/merge-patch+json", '{"spec":{"replicas":4}}'),
        (
            "json",
            "application/json-patch+json",
            '[{"op":"replace","path":"/spec/replicas","value":4}]',
        ),
    ],
)
def test_the_media_type_is_exact_per_patch_type(tmp_path, patch_type, media, body) -> None:
    _, api = _patch(tmp_path, patch_type=patch_type, body_json=body)
    (request,) = api.requests
    assert request.header("Content-Type") == [media]
    assert request.body == body.encode()
    assert request.header("Content-Length") == [str(len(body.encode()))]


def test_the_media_types_cover_exactly_the_executor_patch_types() -> None:
    from safeops.cluster import EXECUTOR_PATCH_TYPES, EXECUTOR_RESOURCES

    assert frozenset(PATCH_MEDIA_TYPES) == EXECUTOR_PATCH_TYPES == {t.value for t in PatchType}
    assert set(API_PATHS) == set(EXECUTOR_RESOURCES)


def test_the_token_is_only_in_the_authorization_header(tmp_path) -> None:
    _, api = _patch(tmp_path)
    (request,) = api.requests
    assert request.header("Authorization") == [f"Bearer {TOKEN}"]
    assert TOKEN not in request.path and TOKEN.encode() not in request.body
    others = [v for k, v in request.headers if k.lower() != "authorization"]
    assert not any(TOKEN in v for v in others)
    assert request.header("Accept") == ["application/json"]


def test_the_connection_goes_to_the_verified_authority_with_the_timeout(tmp_path) -> None:
    _, api = _patch(tmp_path, timeout_s=7.5)
    (conn,) = api.connections
    assert (conn.request.host, conn.request.port) == ("127.0.0.1", PORT)
    assert conn.timeout == 7.5 and conn.closed


@pytest.mark.parametrize(
    "overrides",
    [
        dict(kind="StatefulSet", name="postgres"),
        dict(kind="Secret", name="postgres-credentials"),
        dict(kind="Pod"),
        dict(namespace="safeops-system"),
        dict(namespace="kube-system"),
        dict(namespace="../testbed"),
        dict(name="--all"),
        dict(name="orders/scale"),
        dict(name="orders?x=1"),
        dict(patch_type="apply"),
        dict(body_json="not json"),
        dict(patch_type="json", body_json="{}"),
        dict(body_json="[]"),
        dict(timeout_s=None),
        dict(timeout_s=0),
        dict(timeout_s=-1),
        dict(timeout_s=math.inf),
        dict(timeout_s=math.nan),
        dict(timeout_s=True),
        dict(timeout_s="20"),
    ],
)
def test_the_patch_guard_refuses_before_any_process(tmp_path, overrides) -> None:
    client, api = _client(tmp_path, connect=_never)
    with pytest.raises(GuardError):
        client.patch(**{**ARGS, **overrides})
    assert api.requests == []


def test_the_request_is_checked_with_check_object_directly(tmp_path, monkeypatch) -> None:
    """Never _check_objects or cluster_scoped_kinds: both ask the API server for its kinds."""
    for helper in ("_check_objects", "cluster_scoped_kinds"):
        monkeypatch.setattr(Kubectl, helper, _never)
    seen = []
    import safeops.kube_patch as kp

    real = kp.check_object
    monkeypatch.setattr(kp, "check_object", lambda *a, **k: seen.append(k) or real(*a, **k))
    _patch(tmp_path)
    assert seen == [{"kind": "Deployment", "name": "orders", "namespace": "testbed"}]
    names = _source_names(KUBE_PATCH)
    assert not names & {"_check_objects", "cluster_scoped_kinds", "api-resources", "Kubectl"}


# --- the primitive's signature is a security boundary ---------------------------------------

FORBIDDEN_PARAMETERS = {
    "server",
    "base_url",
    "url",
    "scheme",
    "host",
    "port",
    "authority",
    "token",
    "authorization",
    "headers",
    "kubeconfig",
    "certificate_authority",
    "ca",
    "cafile",
    "cadata",
    "verify",
    "verify_tls",
    "insecure",
    "insecure_skip_tls_verify",
    "tls_server_name",
    "server_hostname",
    "proxy",
    "proxy_url",
    "redirect",
    "follow_redirects",
    "path",
    "raw_path",
}


def test_the_primitive_signature_accepts_only_the_structured_mutation() -> None:
    patch = inspect.signature(ExecutorPatchClient.patch).parameters
    assert list(patch) == [
        "self",
        "kind",
        "name",
        "namespace",
        "patch_type",
        "body_json",
        "timeout_s",
    ]
    assert all(p.kind is p.KEYWORD_ONLY for n, p in patch.items() if n != "self")
    assert all(p.default is p.empty for n, p in patch.items() if n != "self")
    init = inspect.signature(ExecutorPatchClient.__init__).parameters
    assert list(init) == ["self", "cfg", "runner", "connect", "now"]
    writer = inspect.signature(KubernetesWriterTransport.__init__).parameters
    assert list(writer) == ["self", "cfg", "audit", "runner", "connect", "now"]
    for params in (patch, init, writer):
        assert not set(params) & FORBIDDEN_PARAMETERS


# --- no proxy, no redirect -----------------------------------------------------------------


def test_no_proxy_or_redirect_machinery_exists() -> None:
    """Structural: the write path has no urllib opener, urlopen, ProxyHandler, redirect
    handler, tunnel, or environment/system proxy lookup. Proxy absence is a property of the
    code, not something a mutant could switch on."""
    banned = {
        "urlopen",
        "build_opener",
        "OpenerDirector",
        "ProxyHandler",
        "HTTPRedirectHandler",
        "set_tunnel",
        "getproxies",
        "proxy_bypass",
        "environ",
        "getenv",
        "Location",
        "location",
    }
    for path in (KUBE_PATCH, KUBERNETES, REPO_ROOT / "safeops" / "bounded.py"):
        assert not _source_names(path) & banned, path
        assert "urllib.request" not in path.read_text()


PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")


def test_a_hostile_proxy_environment_has_no_effect(tmp_path, monkeypatch) -> None:
    for var in PROXY_ENV:
        monkeypatch.setenv(var, "http://attacker.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "")
    _, api = _patch(tmp_path)
    (conn,) = api.connections
    assert (conn.request.host, conn.request.port) == ("127.0.0.1", PORT)
    real = https_connection("127.0.0.1", PORT, context=conn.context, timeout=5)
    assert (real.host, real.port) == ("127.0.0.1", PORT)
    assert real._tunnel_host is None  # http.client's own tunnel state: never set


def test_the_real_connection_factory_passes_the_explicit_timeout() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    conn = https_connection("127.0.0.1", 1, context=context, timeout=7.5)
    assert isinstance(conn, http.client.HTTPSConnection)
    assert conn.timeout == 7.5 and (conn.host, conn.port) == ("127.0.0.1", 1)
    assert conn.sock is None  # built, never connected


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", ["same-host", "cross-host"])
def test_a_redirect_is_never_followed_and_never_success_or_unsent(tmp_path, status, location):
    target = f"{SERVER}/api/v1/x" if location == "same-host" else "https://attacker.invalid/"
    response = FakeResponse(status, b"")
    response.headers = {"Location": target}
    exchange, api = _patch(tmp_path, script=[response])
    assert exchange.status == status and len(api.requests) == 1  # one request, not re-sent
    assert len(api.connections) == 1
    reply = classify_exchange(exchange)
    assert reply.semantic_class is W.AMBIGUOUS
    assert reply.semantic_class not in (W.SUCCESS, W.NOT_ATTEMPTED)


# --- the send boundary -----------------------------------------------------------------------


@pytest.mark.parametrize("script", ["refused", "tls"])
def test_a_connect_failure_is_pre_send_and_not_attempted(tmp_path, script) -> None:
    exchange, api = _patch(tmp_path, script=[script])
    assert exchange.send_state is SendState.PRE_SEND and api.requests == []
    assert classify_exchange(exchange).semantic_class is W.NOT_ATTEMPTED


@pytest.mark.parametrize(
    "script",
    [
        "drop",
        "timeout",
        "raise",
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        ConnectionResetError(54, "reset"),
        ssl.SSLEOFError("EOF occurred in violation of protocol"),
        FakeResponse(200, b"{}", fail_read=http.client.IncompleteRead(b"{")),
        FakeResponse(200, b"{}", fail_read=TimeoutError("timed out")),
    ],
    ids=["drop", "timeout", "raise", "remote", "reset", "tls-eof", "incomplete", "read-timeout"],
)
def test_a_failure_after_send_started_is_ambiguous_never_not_attempted(tmp_path, script) -> None:
    exchange, api = _patch(tmp_path, script=[script])
    assert exchange.send_state is SendState.SEND_STARTED and len(api.requests) == 1
    reply = classify_exchange(exchange)
    assert reply.semantic_class is W.AMBIGUOUS
    assert TOKEN not in (reply.diagnostic or "") and TOKEN not in (exchange.error or "")


def test_a_timeout_after_send_is_marked_timed_out(tmp_path) -> None:
    exchange, _ = _patch(tmp_path, script=["timeout"])
    assert exchange.timed_out and exchange.status is None
    assert "timed out" in classify_exchange(exchange).diagnostic


def test_an_error_message_holding_the_token_is_scrubbed(tmp_path) -> None:
    def connect(host, port, *, context, timeout):
        raise OSError(f"proxy said: Authorization: Bearer {TOKEN}")

    client, _ = _client(tmp_path, connect=connect)
    exchange = client.patch(**ARGS)
    assert exchange.send_state is SendState.PRE_SEND
    assert TOKEN not in exchange.error and "<redacted>" in exchange.error
    assert TOKEN not in classify_exchange(exchange).diagnostic
    assert TOKEN not in repr(exchange) and TOKEN not in repr(client)


# --- the bounded response body ---------------------------------------------------------------


class Stream:
    def __init__(self, data: bytes, *, overreturn: bool = False):
        self.data, self.overreturn, self.sizes = data, overreturn, []

    def read(self, amt=None):
        self.sizes.append(amt)
        if amt is None or amt < 0:
            raise AssertionError("an unbounded read")
        if self.overreturn:
            out, self.data = self.data, b""
            return out
        out, self.data = self.data[:amt], self.data[amt:]
        return out


@pytest.mark.parametrize(
    "size, truncated", [(0, False), (5, False), (10, False), (11, True), (10_000, True)]
)
def test_read_bounded_bytes_reads_at_most_limit_plus_one(size, truncated) -> None:
    stream = Stream(b"x" * size)
    data, was_truncated = read_bounded_bytes(stream, 10)
    assert data == b"x" * min(size, 10) and was_truncated is truncated
    assert all(type(n) is int and 0 < n <= 11 for n in stream.sizes)
    assert sum(len(b"x" * min(n, size)) for n in stream.sizes) <= 11


def test_read_bounded_bytes_refuses_a_stream_that_returns_more_than_asked() -> None:
    with pytest.raises(ValueError, match="more bytes"):
        read_bounded_bytes(Stream(b"x" * 100, overreturn=True), 10)


@pytest.mark.parametrize("limit", [-1, 1.5, None, True])
def test_read_bounded_bytes_needs_a_non_negative_int_limit(limit) -> None:
    with pytest.raises(ValueError):
        read_bounded_bytes(Stream(b""), limit)


def test_a_huge_2xx_body_is_truncated_never_read_whole_and_ambiguous(tmp_path) -> None:
    response = FakeResponse(200, b'{"kind":"Deployment","metadata":{}}' + b" " * (3 * MAX))
    exchange, _ = _patch(tmp_path, script=[response])
    assert exchange.body_truncated and len(exchange.body_prefix) == MAX
    assert all(type(n) is int and 0 < n <= MAX + 1 for n in response.read_sizes)
    assert sum(response.read_sizes) <= MAX + 1 and response.closed
    reply = classify_exchange(exchange)
    assert reply.semantic_class is W.AMBIGUOUS and "over the cap" in reply.diagnostic


def test_a_server_side_object_over_the_cap_is_ambiguous(tmp_path) -> None:
    exchange, api = _patch(tmp_path, script=["huge"])
    assert exchange.body_truncated
    assert classify_exchange(exchange).semantic_class is W.AMBIGUOUS
    assert api.cluster.get("Deployment", "orders")["spec"]["replicas"] == 4  # it did land


def test_a_body_exactly_at_the_cap_is_complete(tmp_path) -> None:
    head = json.dumps(OBJ).encode()
    body = head + b" " * (MAX - len(head))
    exchange, _ = _patch(tmp_path, script=[FakeResponse(200, body)])
    assert not exchange.body_truncated and len(exchange.body_prefix) == MAX
    assert classify_exchange(exchange).semantic_class is W.SUCCESS


def test_a_huge_403_is_still_definite_with_a_bounded_diagnostic() -> None:
    body = status_body(403, "Forbidden", "x" * (2 * MAX))[:MAX]
    reply = classify_exchange(_exchange(403, body, truncated=True))
    assert reply.semantic_class is W.DEFINITE_FAILURE
    assert len(reply.diagnostic) <= MAX_DISCARD_ERROR and "[truncated]" in reply.diagnostic


# --- the hardened parser, shared from the neutral module -------------------------------------

UNSAFE = [
    b'{"kind":"Deployment","metadata":{},"kind":"Service"}',
    b'{"kind":"Deployment","metadata":{"name":"a","name":"b"}}',
    b'{"kind":"Deployment","metadata":{},"x":NaN}',
    b'{"kind":"Deployment","metadata":{},"x":Infinity}',
    b'{"kind":"Deployment","metadata":{},"x":1e400}',
    b'{"kind":"Deployment","metadata":{},"x":' + b"9" * 5000 + b"}",
    b'{"kind":"Deployment","metadata":{},"x":' + b"[" * 100_000 + b"]" * 100_000 + b"}",
]
UNSAFE_IDS = ["dup-top", "dup-nested", "nan", "infinity", "overflow", "huge-int", "nesting"]


@pytest.mark.parametrize("body", UNSAFE, ids=UNSAFE_IDS)
def test_the_neutral_parser_refuses_unsafe_json(body) -> None:
    with pytest.raises(MalformedJSON):
        strict_json_object(body.decode())


@pytest.mark.parametrize("text", ["[]", "1", '"x"', "null", "true", "", "{} {}"])
def test_the_neutral_parser_requires_one_top_level_object(text) -> None:
    with pytest.raises(MalformedJSON):
        strict_json_object(text)


def test_the_agent_parser_re_exports_the_same_neutral_parser() -> None:
    import safeops.agent.parser as parser

    assert parser.strict_json_object is strict_json_object
    assert parser._Malformed is MalformedJSON and issubclass(MalformedJSON, ValueError)
    assert strict_json_object(' {"a": 1.5, "b": [1, 2]} ') == {"a": 1.5, "b": [1, 2]}


@pytest.mark.parametrize("body", UNSAFE, ids=UNSAFE_IDS)
def test_an_unsafe_2xx_object_is_ambiguous_never_success(body) -> None:
    assert classify_exchange(_exchange(200, body)).semantic_class is W.AMBIGUOUS


@pytest.mark.parametrize("body", UNSAFE, ids=UNSAFE_IDS)
def test_an_unsafe_422_body_is_definite_never_invalid_action(body) -> None:
    unsafe = body.replace(b'"kind":"Deployment"', b'"kind":"Status","reason":"Invalid"', 1)
    assert classify_exchange(_exchange(422, unsafe)).semantic_class is W.DEFINITE_FAILURE


# --- the five classes ----------------------------------------------------------------------

INVALID = status_body(422, "Invalid", 'Deployment.apps "orders" is invalid: spec: Invalid value')


@pytest.mark.parametrize(
    "status, body, truncated, cls",
    [
        (200, json.dumps(OBJ).encode(), False, W.SUCCESS),
        (201, json.dumps(OBJ).encode(), False, W.SUCCESS),
        (200, json.dumps(OBJ).encode(), True, W.AMBIGUOUS),
        (200, b"not json", False, W.AMBIGUOUS),
        (200, b"[]", False, W.AMBIGUOUS),
        (200, b'{"metadata":{}}', False, W.AMBIGUOUS),
        (200, b'{"kind":"Deployment"}', False, W.AMBIGUOUS),
        (200, b"\xff\xfe{}", False, W.AMBIGUOUS),
        (200, b"", False, W.AMBIGUOUS),
        (422, INVALID, False, W.INVALID_ACTION),
        (422, INVALID, True, W.DEFINITE_FAILURE),
        (422, status_body(422, "Unprocessable", "x"), False, W.DEFINITE_FAILURE),
        (422, b"not json", False, W.DEFINITE_FAILURE),
        (
            422,
            json.dumps({"kind": "Deployment", "reason": "Invalid"}).encode(),
            False,
            W.DEFINITE_FAILURE,
        ),
        (422, b"", False, W.DEFINITE_FAILURE),
        (400, b"", False, W.DEFINITE_FAILURE),
        (401, b"", False, W.DEFINITE_FAILURE),
        (403, status_body(403, "Forbidden", "no"), False, W.DEFINITE_FAILURE),
        (403, b"{", True, W.DEFINITE_FAILURE),
        (404, b"", False, W.DEFINITE_FAILURE),
        (409, b"", False, W.DEFINITE_FAILURE),
        (429, b"", False, W.DEFINITE_FAILURE),
        (500, b"", False, W.AMBIGUOUS),
        (502, b"", False, W.AMBIGUOUS),
        (503, b"", False, W.AMBIGUOUS),
        (504, b"", False, W.AMBIGUOUS),
        (500, json.dumps(OBJ).encode(), True, W.AMBIGUOUS),
        (301, b"", False, W.AMBIGUOUS),
        (304, b"", False, W.AMBIGUOUS),
        (307, b"", False, W.AMBIGUOUS),
        (308, b"", False, W.AMBIGUOUS),
        (100, b"", False, W.AMBIGUOUS),
        (600, b"", False, W.AMBIGUOUS),
        (999, b"", False, W.AMBIGUOUS),
    ],
)
def test_the_http_outcome_decides_the_class(status, body, truncated, cls) -> None:
    reply = classify_exchange(_exchange(status, body, truncated=truncated))
    assert reply.semantic_class is cls
    if cls is W.SUCCESS:
        assert reply.returned_object == OBJ


def test_a_422_invalid_status_is_invalid_action_with_the_server_s_message() -> None:
    reply = classify_exchange(_exchange(422, INVALID))
    assert reply.semantic_class is W.INVALID_ACTION
    assert reply.diagnostic.startswith("HTTP 422: ") and "is invalid" in reply.diagnostic


def test_a_definite_4xx_survives_a_failed_body_read() -> None:
    exchange = _exchange(403, b"", error="IncompleteRead: 0 bytes read")
    assert classify_exchange(exchange).semantic_class is W.DEFINITE_FAILURE
    assert classify_exchange(
        _exchange(200, json.dumps(OBJ).encode(), error="x")
    ).semantic_class is (W.AMBIGUOUS)


def test_no_response_after_send_started_is_ambiguous() -> None:
    lost = _exchange(None, error="ConnectionResetError: reset")
    assert classify_exchange(lost).semantic_class is W.AMBIGUOUS
    timed = _exchange(None, error="TimeoutError: timed out", timed_out=True)
    assert "timed out" in classify_exchange(timed).diagnostic


def test_pre_send_is_not_attempted() -> None:
    exchange = PatchExchange(SendState.PRE_SEND, error="ConnectionRefusedError: refused")
    assert classify_exchange(exchange).semantic_class is W.NOT_ATTEMPTED


def test_the_exchange_holds_consistent_evidence_only() -> None:
    with pytest.raises(ValueError):
        PatchExchange(SendState.PRE_SEND, status=200, response_received=True)
    with pytest.raises(ValueError):
        PatchExchange(SendState.SEND_STARTED, status=200)  # a status means a response arrived
    with pytest.raises(ValueError):
        PatchExchange(SendState.SEND_STARTED, response_received=True)
    with pytest.raises(ValueError):
        _exchange(200, b"x" * (MAX + 1))
    with pytest.raises(TypeError):
        PatchExchange("pre_send")
    assert "body_prefix" not in repr(_exchange(200, b"secret-ish body"))


def test_every_diagnostic_is_bounded() -> None:
    body = status_body(403, "Forbidden", "y" * 50_000)
    assert len(classify_exchange(_exchange(403, body)).diagnostic) <= MAX_DISCARD_ERROR
    long_error = _exchange(None, error="E" * 50_000)
    assert len(classify_exchange(long_error).diagnostic) <= MAX_DISCARD_ERROR


# --- the writer: one PATCH, no retry, no kubectl fallback ------------------------------------


def _scale(replicas=4):
    return prepared(
        ActionType.SCALE_DEPLOYMENT,
        "Deployment",
        "testbed",
        "orders",
        PatchType.MERGE,
        {"spec": {"replicas": replicas}},
        Check.REPLICAS,
        replicas,
    )


def _writer(tmp_path, *, executor="valid", script=()):
    cfg = credentials(scratch_cfg(tmp_path), executor=executor)
    api = ProcessCluster(script=script)
    writer = KubernetesWriterTransport(cfg, RecordingAudit(), runner=api, connect=api.connect)
    return writer, api


@pytest.mark.parametrize("script", ["server", "timeout", "drop", "raise", "redirect", "huge"])
def test_an_ambiguous_write_is_never_retried(tmp_path, script) -> None:
    writer, api = _writer(tmp_path, script=[script, "success"])
    request = _scale()
    writer.preflight((request,))
    assert writer.write(request).semantic_class is W.AMBIGUOUS
    assert len(api.connections) == 1 and len(api.requests) == 1
    assert api.script == ["success"]  # the second outcome was never asked for


@pytest.mark.parametrize("executor", ["valid", "missing", "expired"])
def test_no_kubectl_fallback_exists(tmp_path, executor) -> None:
    for script in (["success"], ["server"], ["refused"], ["forbidden"]):
        writer, api = _writer(tmp_path, executor=executor, script=script)
        request = _scale()
        writer.preflight((request,))
        writer.write(request)
        assert not [c for c in api.calls if c[0] == "kubectl"]  # never a kubectl process
    names = _source_names(KUBERNETES)
    assert "patch" not in {n for n in names if n.startswith("kubectl")}
    assert not hasattr(Kubectl, "patch")


def test_a_credential_that_appears_after_preflight_is_not_attempted_without_kubectl(tmp_path):
    """Unavailable at preflight, so no client was ever verified; issued before the write.
    The write is NOT_ATTEMPTED, and nothing else runs: no kubectl, no connection."""
    writer, api = _writer(tmp_path, executor="missing")
    request = _scale()
    writer.preflight((request,))
    credentials(writer.cfg)  # the credential shows up now
    reply = writer.write(request)
    assert reply.semantic_class is W.NOT_ATTEMPTED and "never verified" in reply.diagnostic
    assert api.calls == [] and api.connections == [] and writer._client is None


def test_a_success_returns_the_object_and_its_identity(tmp_path) -> None:
    writer, api = _writer(tmp_path)
    request = _scale(6)
    writer.preflight((request,))
    reply = writer.write(request)
    assert reply.semantic_class is W.SUCCESS
    obj = reply.returned_object
    assert (obj["kind"], obj["metadata"]["name"], obj["metadata"]["namespace"]) == (
        "Deployment",
        "orders",
        "testbed",
    )
    assert obj["spec"]["replicas"] == 6


# --- the reader is untouched by the direct PATCH ---------------------------------------------


def _class_names(path: Path, cls: str) -> set[str]:
    tree = ast.parse(path.read_text())
    (node,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls]
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def test_the_reader_never_depends_on_the_direct_patch(tmp_path) -> None:
    names = _class_names(KUBERNETES, "KubernetesReaderTransport")
    assert not names & {"ExecutorPatchClient", "https_connection", "classify_exchange", "_connect"}
    assert {"Kubectl", "READER"} <= names
    cfg = credentials(scratch_cfg(tmp_path))
    api = ProcessCluster()
    reader = KubernetesReaderTransport(cfg, runner=api)
    assert reader.get("Deployment", "orders", "testbed", 10).obj["metadata"]["name"] == "orders"
    assert reader.list("ReplicaSet", "testbed", 10).obj["items"]
    assert api.requests == [] and api.connections == []  # kubectl, never the HTTPS primitive
    assert len(api.gets) == 2


def test_the_direct_primitive_has_no_read_method() -> None:
    """Executor-write-only: no GET, LIST or other request method exists in the module."""
    constants = {
        n.value
        for n in ast.walk(ast.parse(KUBE_PATCH.read_text()))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "PATCH" in constants
    assert not constants & {"GET", "LIST", "WATCH", "POST", "PUT", "DELETE", "HEAD"}
    public = {n for n in dir(ExecutorPatchClient) if not n.startswith("_")}
    assert public == {"patch", "authority"}


def test_the_test_ca_fixture_is_a_certificate_only() -> None:
    import base64

    pem = base64.b64decode(TEST_CA_DATA).decode()
    assert pem.startswith("-----BEGIN CERTIFICATE-----") and "PRIVATE KEY" not in pem
