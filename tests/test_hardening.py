"""Whole-project hardening pass (decision 0021). Offline; live counterparts are in
tests/cluster/test_lifecycle_live.py.

The teardown tests never run kind or Docker: both are injected fakes, and the cluster
config points at a temporary directory, so the real .safeops/ is never touched.
"""

from __future__ import annotations

import dataclasses
import subprocess

import pytest

from safeops.cluster import CommandError, load_cluster_config
from safeops.environment import node as node_mod
from safeops.environment.manager import INVENTORY, EnvironmentManager, unexpected_objects
from safeops.environment.node import NodeCheckError, NodeInspector, parse_qdisc_output

# --- 1. qdisc inspection fails closed ----------------------------------------------------------

CLEAN = (
    "net:[4026531840] qdisc noqueue 0: dev lo root refcnt 2\n"
    "net:[4026531840] qdisc noqueue 0: dev eth0 root refcnt 2\n"
    "net:[4026532562] qdisc noqueue 0: dev lo root refcnt 2\n"
)


def test_clean_qdiscs_are_no_residue() -> None:
    assert parse_qdisc_output(CLEAN) == []


@pytest.mark.parametrize("kind", ["netem", "tbf"])
def test_planted_residue_is_found(kind) -> None:
    out = CLEAN + f"net:[4026532562] qdisc {kind} 8001: dev eth0 root refcnt 2 delay 200ms\n"
    (found,) = parse_qdisc_output(out)
    assert found.startswith(f"{kind} qdisc in network namespace net:[4026532562]")


@pytest.mark.parametrize("out", ["", "\n", "   \n"])
def test_empty_inspection_output_is_a_failure_not_clean(out) -> None:
    with pytest.raises(NodeCheckError, match="no qdiscs at all"):
        parse_qdisc_output(out)


def test_only_vanished_namespaces_is_a_failure_not_clean() -> None:
    with pytest.raises(NodeCheckError, match="no qdiscs at all"):
        parse_qdisc_output(f"{node_mod.QDISC_GONE_MARKER} net:[1]\n")


def test_a_vanished_namespace_beside_real_ones_is_fine() -> None:
    assert parse_qdisc_output(CLEAN + f"{node_mod.QDISC_GONE_MARKER} net:[4026539999]\n") == []


@pytest.mark.parametrize(
    "marker_line",
    [
        "SAFEOPS_QDISC_INSPECTION_ERROR: net:[4026532562] pid 4242: tc printed no qdiscs",
        "SAFEOPS_QDISC_INSPECTION_ERROR: /var/run/netns/cni-x: nsenter: Operation not permitted",
    ],
)
def test_a_per_namespace_failure_marker_is_a_failure(marker_line) -> None:
    with pytest.raises(NodeCheckError, match="qdisc inspection failed"):
        parse_qdisc_output(CLEAN + marker_line + "\n")


def test_ordinary_words_like_error_are_not_the_marker() -> None:
    """Only the exact SafeOps marker counts; tc output is never searched for 'error'."""
    assert parse_qdisc_output(CLEAN + "net:[1] qdisc noqueue 0: dev error0 root refcnt 2\n") == []


def test_the_script_prints_the_markers_the_parser_reads() -> None:
    assert f"ERR='{node_mod.QDISC_ERROR_MARKER}'" in node_mod._QDISC_SCRIPT
    assert f"GONE='{node_mod.QDISC_GONE_MARKER}'" in node_mod._QDISC_SCRIPT
    assert "2>/dev/null | sed" not in node_mod._QDISC_SCRIPT  # tc errors are no longer swallowed


class _Docker:
    """A fake `docker` for NodeInspector: labels, the published port, then exec output."""

    def __init__(self, exec_out: str, cluster="safeops", role="control-plane"):
        self.exec_out, self.labels = exec_out, f"{cluster}|{role}"

    def __call__(self, args, **kw):
        if args[1] == "inspect":
            return subprocess.CompletedProcess(args, 0, self.labels + "\n", "")
        if args[1] == "port":
            return subprocess.CompletedProcess(args, 0, "127.0.0.1:6443\n", "")
        return subprocess.CompletedProcess(args, 0, self.exec_out, "")


def test_node_inspector_refuses_to_call_an_empty_inspection_clean() -> None:
    insp = NodeInspector(load_cluster_config(), _Docker(""), "https://127.0.0.1:6443")
    with pytest.raises(NodeCheckError):
        insp.qdisc_residue()
    assert (
        NodeInspector(
            load_cluster_config(), _Docker(CLEAN), "https://127.0.0.1:6443"
        ).qdisc_residue()
        == []
    )


# --- 2. legacy core/v1 Endpoints are inventoried ----------------------------------------------


def _endpoints(name, labels=None, owners=None) -> dict:
    meta = {
        "name": name,
        "labels": labels
        if labels is not None
        else {
            "app": name.removesuffix("-svc"),
            "endpoints.kubernetes.io/managed-by": "endpoint-controller",
        },
    }
    if owners:
        meta["ownerReferences"] = owners
    return {"metadata": meta}


SERVICES = {"gateway-svc", "orders-svc"}


def _flagged(*objs) -> list:
    return unexpected_objects({"endpoints": list(objs)}, set(), set(), set(), SERVICES)


def test_endpoints_are_inventoried_and_deletable() -> None:
    assert INVENTORY["endpoints"] == ("Endpoints", True)


def test_the_controller_s_endpoints_for_a_desired_service_are_expected() -> None:
    assert _flagged(_endpoints("gateway-svc"), _endpoints("orders-svc")) == []


@pytest.mark.parametrize(
    "obj",
    [
        _endpoints("rogue-ep"),  # the controller's label, but no such Service
        _endpoints("orders-svc", labels={"app": "orders"}),  # hand-made: not controller-managed
        _endpoints("orders-svc", labels={"endpoints.kubernetes.io/managed-by": "someone-else"}),
        _endpoints(
            "orders-svc",
            owners=[{"kind": "Service", "name": "orders-svc", "controller": True}],
        ),  # the controller sets no owner; a different shape is not its object
    ],
    ids=["rogue name", "no controller label", "other manager", "unexpected owner"],
)
def test_any_other_endpoints_object_is_unexpected(obj) -> None:
    assert _flagged(obj) == [("endpoints", "Endpoints", obj["metadata"]["name"], True)]


def test_endpoint_slices_are_classified_as_before() -> None:
    slice_ = {
        "metadata": {
            "name": "orders-svc-abc",
            "labels": {"endpointslice.kubernetes.io/managed-by": "endpointslice-controller.k8s.io"},
            "ownerReferences": [{"kind": "Service", "name": "orders-svc", "controller": True}],
        }
    }
    items = {"endpointslices.discovery.k8s.io": [slice_]}
    assert unexpected_objects(items, set(), set(), set(), SERVICES) == []


# --- 3. cluster-down proves identity by kind labels, never by API reachability --------------------


@pytest.fixture
def scratch_cfg(tmp_path, monkeypatch):
    """The real config, with every local path moved into a temporary directory."""
    from safeops import bootstrap

    real = load_cluster_config()
    state = tmp_path / ".safeops"
    state.mkdir()
    cfg = dataclasses.replace(
        real,
        kubeconfig_path=state / "kubeconfig",
        harness_kubeconfig_path=state / "experiment-controller.kubeconfig",
        harness_token_meta_path=state / "experiment-controller.token.json",
    )
    monkeypatch.setattr(bootstrap, "load_cluster_config", lambda: cfg)
    return cfg


class _Kind:
    def __init__(self, clusters: str):
        self.clusters, self.calls = clusters, []

    def __call__(self, args, **kw):
        self.calls.append(args)
        if args[0] == "kubectl":
            raise CommandError("the API server is unreachable")  # teardown must not need it
        if args[:3] == ["kind", "get", "clusters"]:
            return self.clusters
        return ""

    @property
    def deleted(self) -> bool:
        return any(a[:3] == ["kind", "delete", "cluster"] for a in self.calls)


def _labels(out: str, rc: int = 0):
    def docker(args, **kw):
        assert args[:2] == ["docker", "inspect"] and args[2] == "safeops-control-plane"
        return subprocess.CompletedProcess(
            args, rc, out, "" if rc == 0 else "Error: No such object"
        )

    return docker


def test_teardown_proceeds_on_correct_labels_with_the_api_server_down(scratch_cfg) -> None:
    from safeops import bootstrap

    kind = _Kind("safeops\n")
    bootstrap.down(tool=kind, docker=_labels("safeops|control-plane\n"))
    assert kind.deleted and not any(a[0] == "kubectl" for a in kind.calls)


@pytest.mark.parametrize(
    ("docker", "match"),
    [
        (_labels("someone-else|control-plane\n"), "cluster='someone-else'"),
        (_labels("safeops|worker\n"), "role='worker'"),
        (_labels("", rc=1), "could not be inspected"),
        (_labels("garbled\n"), "unreadable kind labels"),
        (_labels("|\n"), "cluster=''"),
    ],
    ids=["wrong cluster", "wrong role", "uninspectable", "garbled", "no labels"],
)
def test_teardown_refuses_without_identity_proof(scratch_cfg, docker, match) -> None:
    from safeops import bootstrap

    kind = _Kind("safeops\n")
    with pytest.raises(CommandError, match=match) as exc:
        bootstrap.down(tool=kind, docker=docker)
    assert not kind.deleted
    assert "safeops-control-plane" in str(exc.value) and "expected cluster='safeops'" in str(
        exc.value
    )


def test_teardown_of_an_absent_cluster_is_idempotent(scratch_cfg, capsys) -> None:
    from safeops import bootstrap

    def no_docker(args, **kw):
        raise AssertionError("nothing to verify when there is no cluster")

    kind = _Kind("other-cluster\n")
    bootstrap.down(tool=kind, docker=no_docker)
    assert not kind.deleted and "does not exist" in capsys.readouterr().out


def test_node_inspection_and_teardown_share_one_identity_check() -> None:
    import inspect

    from safeops import bootstrap

    for fn in (NodeInspector.verify_identity, bootstrap.down):
        assert "verify_control_plane" in inspect.getsource(fn)
    assert "io.x-k8s.kind" not in inspect.getsource(bootstrap)  # no second label check


# --- 8. a rebuild drops clients built from the old credentials (checked, not changed) ------------


def test_rebuild_drops_cached_clients_but_keeps_injected_ones(tmp_path) -> None:
    from safeops.environment.manager import CleanStateResult

    real = load_cluster_config()
    cfg = dataclasses.replace(real, kubeconfig_path=tmp_path / "kubeconfig")

    def env_with(**injected):
        env = EnvironmentManager(rebuild=lambda: None, image_check=False, **injected)
        env._cfg = cfg
        env.verify_clean_state = lambda: CleanStateResult(True, ())
        return env

    env = env_with()
    env._kubectl, env._node = object(), object()  # cached from the old cluster
    env.quarantine_and_rebuild("test")
    assert env._kubectl is None and env._node is None  # rebuilt lazily with new credentials

    k, n = object(), object()
    env = env_with(kubectl=k, node=n)
    env.quarantine_and_rebuild("test")
    assert env._kubectl is k and env._node is n  # a caller's doubles are never dropped
