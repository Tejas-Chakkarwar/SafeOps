"""Exact cluster-instance ownership for destructive teardown (decision 0022). Offline.

kind and Docker are injected fakes and every local path lives in a temporary directory:
no test here deletes, or could delete, a real cluster. Teardown's independence from
Kubernetes is proven here, structurally, never by breaking a live API server.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from safeops import bootstrap, kind_identity
from safeops.cluster import CommandError, load_cluster_config
from safeops.environment.manager import EnvironmentManager, QuarantineFailed
from safeops.kind_identity import FORCE_FLAG, identity_path

OURS = "a" * 64
OTHER = "b" * 64
IDENTITY = {
    "schema_version": 1,
    "cluster_name": "safeops",
    "control_plane_container": "safeops-control-plane",
    "container_id": OURS,
}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """The real config, with every local path moved into a temporary state directory."""
    from tests.conftest import scratch_cluster_config

    state = tmp_path / ".safeops"
    state.mkdir()
    c = scratch_cluster_config(state)
    monkeypatch.setattr(bootstrap, "load_cluster_config", lambda: c)
    return c


def _pin(cfg, record=IDENTITY) -> None:
    identity_path(cfg.state_dir).write_text(
        record if isinstance(record, str) else json.dumps(record)
    )


class Kind:
    """A fake `kind`. Teardown must never need kubectl or helm, so they raise here."""

    def __init__(self, cfg, present=True, delete_fails=False, delete_is_a_lie=False):
        self.cfg, self.present = cfg, present
        self.delete_fails, self.delete_is_a_lie = delete_fails, delete_is_a_lie
        self.calls: list[list[str]] = []
        self.identity_at_delete: bool | None = None

    def __call__(self, args, **kw):
        self.calls.append(args)
        if args[0] in ("kubectl", "helm"):
            raise AssertionError(f"teardown used {args[0]}: it must not need Kubernetes")
        if args[:3] == ["kind", "get", "clusters"]:
            return "safeops\n" if self.present else "other\n"
        if args[:3] == ["kind", "delete", "cluster"]:
            self.identity_at_delete = identity_path(self.cfg.state_dir).exists()
            if self.delete_fails:
                raise CommandError("kind delete cluster failed: docker daemon not responding")
            if not self.delete_is_a_lie:
                self.present = False
            return ""
        raise AssertionError(f"unexpected tool call {args}")

    @property
    def deleted(self) -> bool:
        return any(a[:3] == ["kind", "delete", "cluster"] for a in self.calls)


def docker(container_id=OURS, cluster="safeops", role="control-plane", rc=0, out=None):
    calls = []

    def run(args, **kw):
        calls.append(args)
        assert args[:3] == ["docker", "inspect", "safeops-control-plane"], args
        text = out if out is not None else f"{container_id}|{cluster}|{role}\n"
        return subprocess.CompletedProcess(
            args, rc, text if rc == 0 else "", "Error: No such object"
        )

    run.calls = calls
    return run


def _refusal(cfg, kind, d, match) -> str:
    with pytest.raises(CommandError, match=match) as exc:
        bootstrap.down(tool=kind, docker=d)
    assert not kind.deleted
    return str(exc.value)


# --- 1-9: normal teardown ------------------------------------------------------------------------


def test_absent_cluster_is_idempotent_and_drops_a_stale_record(cfg, capsys) -> None:
    _pin(cfg)

    def no_docker(*a, **k):
        raise AssertionError("nothing to verify when there is no cluster")

    kind = Kind(cfg, present=False)
    bootstrap.down(tool=kind, docker=no_docker)
    assert not kind.deleted and "does not exist" in capsys.readouterr().out
    assert not identity_path(cfg.state_dir).exists()  # removed once absence is established


def test_exact_owned_instance_is_deleted_and_only_then_unpinned(cfg) -> None:
    _pin(cfg)
    kind = Kind(cfg)
    bootstrap.down(tool=kind, docker=docker())
    assert kind.deleted and kind.identity_at_delete is True  # still pinned while deleting
    assert not identity_path(cfg.state_dir).exists()  # removed after the confirmed delete


def test_correct_labels_but_another_container_id_is_refused(cfg) -> None:
    _pin(cfg)
    msg = _refusal(cfg, Kind(cfg), docker(container_id=OTHER), "not the cluster SafeOps created")
    for part in (OURS, OTHER, str(identity_path(cfg.state_dir)), "safeops-control-plane"):
        assert part in msg
    assert "cluster='safeops'" in msg and "role='control-plane'" in msg
    assert identity_path(cfg.state_dir).exists()


@pytest.mark.parametrize(
    ("d", "match"),
    [
        (docker(cluster="someone-else"), "cluster='someone-else'"),
        (docker(role="worker"), "role='worker'"),
        (docker(rc=1), "could not be inspected"),
        (docker(out="garbled\n"), "unreadable kind labels"),
        (docker(container_id="abc123"), "unreadable container ID"),
    ],
    ids=["wrong cluster label", "wrong role", "uninspectable", "garbled", "short ID"],
)
def test_without_standard_kind_identity_teardown_refuses(cfg, d, match) -> None:
    _pin(cfg)
    msg = _refusal(cfg, Kind(cfg), d, match)
    assert "safeops-control-plane" in msg and "expected cluster='safeops'" in msg
    assert identity_path(cfg.state_dir).exists()


def test_a_missing_record_is_refused_with_the_legacy_migration_guidance(cfg) -> None:
    msg = _refusal(cfg, Kind(cfg), docker(), "no exact ownership record")
    assert "legacy pre-pin SafeOps cluster" in msg
    assert f"python -m safeops.bootstrap down {FORCE_FLAG}" in msg and "make cluster-up" in msg
    assert "verify independently that it is safe to delete" in msg
    assert "kind delete cluster --name safeops" in msg
    assert "bypasses SafeOps ownership verification" in msg
    assert str(identity_path(cfg.state_dir)) in msg and OURS in msg  # the current ID


@pytest.mark.parametrize(
    "record",
    [
        "{not json",
        "",
        json.dumps([IDENTITY]),
        json.dumps({k: v for k, v in IDENTITY.items() if k != "container_id"}),
        json.dumps({**IDENTITY, "container_id": OURS[:12]}),  # a short display ID
        json.dumps({**IDENTITY, "container_id": OURS.upper()}),
        json.dumps({**IDENTITY, "container_id": None}),
        json.dumps({**IDENTITY, "cluster_name": "other"}),
        json.dumps({**IDENTITY, "control_plane_container": "other-control-plane"}),
        json.dumps({**IDENTITY, "schema_version": 2}),
        json.dumps({**IDENTITY, "schema_version": "1"}),
        json.dumps({**IDENTITY, "schema_version": True}),
        json.dumps({**IDENTITY, "adopted": True}),
    ],
    ids=[
        "malformed",
        "empty",
        "not an object",
        "no container_id",
        "short ID",
        "uppercase ID",
        "null ID",
        "other cluster",
        "other container",
        "future schema",
        "string schema",
        "bool schema",
        "unknown field",
    ],
)
def test_a_malformed_stale_or_incomplete_record_is_refused(cfg, record) -> None:
    _pin(cfg, record)
    msg = _refusal(cfg, Kind(cfg), docker(), "ownership")
    assert "can't be proven" in msg or "not the cluster" in msg
    assert identity_path(cfg.state_dir).read_text() == record  # left exactly as found


# --- 10-11: the explicit operator override, argv only -------------------------------------------


def _main(monkeypatch, cfg, argv, kind, d) -> int:
    monkeypatch.setattr(bootstrap, "_tool", kind)
    monkeypatch.setattr(subprocess, "run", d)
    return bootstrap.main(argv)


def test_the_argv_override_deletes_a_legacy_unpinned_cluster(cfg, monkeypatch, capsys) -> None:
    kind = Kind(cfg)
    assert _main(monkeypatch, cfg, ["down", FORCE_FLAG], kind, docker()) == 0
    assert kind.deleted and "WARNING" in capsys.readouterr().err


def test_the_override_skips_only_the_id_match(cfg, monkeypatch) -> None:
    _pin(cfg)
    kind = Kind(cfg)
    assert _main(monkeypatch, cfg, ["down", FORCE_FLAG], kind, docker(container_id=OTHER)) == 0
    assert kind.deleted
    kind = Kind(cfg)  # the labels are still required under the override
    assert _main(monkeypatch, cfg, ["down", FORCE_FLAG], kind, docker(cluster="x")) == 1
    assert not kind.deleted


def test_normal_argv_never_enables_the_override(cfg, monkeypatch, capsys) -> None:
    kind = Kind(cfg)
    assert _main(monkeypatch, cfg, ["down"], kind, docker()) == 1
    assert not kind.deleted and "no exact ownership record" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["down", "--force"],
        ["down", "--force-unverified"],
        [FORCE_FLAG, "down"],
        ["up", FORCE_FLAG],
        ["down", FORCE_FLAG, FORCE_FLAG],
        ["down", f"{FORCE_FLAG}=1"],
    ],
)
def test_only_the_exact_argv_form_is_accepted(cfg, monkeypatch, argv) -> None:
    kind = Kind(cfg)
    assert _main(monkeypatch, cfg, argv, kind, docker()) == 2  # a usage error, nothing run
    assert kind.calls == []


ENVIRONMENT_ATTEMPTS = {
    "SAFEOPS_FORCE_UNVERIFIED_OWNERSHIP": "1",
    "SAFEOPS_FORCE_UNVERIFIED": "1",
    "FORCE_UNVERIFIED_OWNERSHIP": "1",
    "SAFEOPS_FORCE": "true",
    "SAFEOPS_ARGS": FORCE_FLAG,
    "MAKEFLAGS": FORCE_FLAG,
}


def test_the_environment_cannot_enable_the_override(cfg, monkeypatch) -> None:
    for name, value in ENVIRONMENT_ATTEMPTS.items():
        monkeypatch.setenv(name, value)
    kind = Kind(cfg)
    assert _main(monkeypatch, cfg, ["down"], kind, docker()) == 1
    assert not kind.deleted
    seen = {}
    monkeypatch.setattr(bootstrap, "down", lambda **kw: seen.update(kw))
    assert bootstrap.main(["down"]) == 0 and seen == {}  # called with no override at all


def test_no_environment_or_config_path_to_the_override_exists() -> None:
    """Structural: the contract is that no such path exists, so none may be added."""
    sources = [
        inspect.getsource(kind_identity),
        inspect.getsource(bootstrap.down),
        inspect.getsource(bootstrap.main),
    ]
    for src in sources:
        assert "os.environ" not in src and "getenv" not in src and "environ[" not in src
    fields = {f.name for f in dataclasses.fields(load_cluster_config())}
    assert not {f for f in fields if "force" in f or "owner" in f}
    callers = [
        f
        for f in (kind_identity.__file__, bootstrap.__file__)
        if "force_unverified_ownership=True" in Path(f).read_text()
    ]
    assert callers == [bootstrap.__file__]  # only main() arms it
    assert inspect.getsource(bootstrap.main).count("force_unverified_ownership=True") == 1


# --- 12: quarantine never uses the override, and fails closed with the guidance ------------------


def test_default_rebuild_never_passes_the_override_and_surfaces_the_refusal(
    tmp_path, monkeypatch
) -> None:
    for name, value in ENVIRONMENT_ATTEMPTS.items():
        monkeypatch.setenv(name, value)  # an operator shell that once exported something
    children = []
    refusal = (
        "[cluster-down] FAILED: refusing to delete kind cluster 'safeops': no exact ownership"
        " record at /x/.safeops/cluster-identity.json: this may be a legacy pre-pin SafeOps"
        f" cluster ... `uv run python -m safeops.bootstrap down {FORCE_FLAG}`\n"
    )

    def run(args, **kw):
        children.append((args, kw))
        return subprocess.CompletedProcess(args, 1, None, refusal)

    monkeypatch.setattr(subprocess, "run", run)
    env = EnvironmentManager(image_check=False)
    env._cfg = dataclasses.replace(load_cluster_config(), kubeconfig_path=tmp_path / "kc")
    with pytest.raises(QuarantineFailed) as exc:
        env.quarantine_and_rebuild("pre-run: half reset")
    assert children[0][0] == [sys.executable, "-m", "safeops.bootstrap", "down"]
    assert all(FORCE_FLAG not in " ".join(a) for a, _ in children)
    assert len(children) == 1  # no fallback attempt of any kind, and up never ran
    assert "legacy pre-pin" in str(exc.value) and FORCE_FLAG in str(exc.value)
    assert env._marker.exists()  # the worker stays quarantined


def test_default_rebuild_argv_is_fixed_in_source() -> None:
    src = inspect.getsource(EnvironmentManager._default_rebuild)
    assert '[sys.executable, "-m", "safeops.bootstrap", command]' in src
    assert "force" not in src.split('"""')[2].lower()


# --- 13: cluster-up pins the new cluster at once, and never adopts one ---------------------------


def test_cluster_up_pins_identity_right_after_kind_create(cfg, monkeypatch) -> None:
    events = []
    for name in ("_preflight", "verified_scope"):
        monkeypatch.setattr(bootstrap, name, lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "pull_charts", lambda c: {})
    monkeypatch.setattr(bootstrap, "build_images", lambda c: {})

    def tool(args, **kw):
        events.append(" ".join(args[:3]))
        if args[:3] == ["kind", "create", "cluster"]:
            cfg.kubeconfig_path.write_text("admin")
        return ""

    def run(args, **kw):
        events.append("docker inspect")
        return docker()(args, **kw)

    class Later:
        def __init__(self, *a, **k):
            events.append("next bootstrap step")
            raise CommandError("the API server never became ready")

    monkeypatch.setattr(bootstrap, "_tool", tool)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(bootstrap, "Kubectl", Later)
    with pytest.raises(CommandError, match="never became ready"):
        bootstrap.up()
    assert events == ["kind create cluster", "docker inspect", "next bootstrap step"]
    assert json.loads(identity_path(cfg.state_dir).read_text()) == IDENTITY  # survives the failure
    assert oct(identity_path(cfg.state_dir).stat().st_mode & 0o777) == "0o600"


def test_cluster_up_never_adopts_an_existing_same_name_cluster(cfg, monkeypatch) -> None:
    calls = []

    def tool(args, **kw):
        calls.append(args)
        return "safeops\n" if args[:3] == ["kind", "get", "clusters"] else ""

    monkeypatch.setattr(bootstrap, "_tool", tool)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", docker())
    with pytest.raises(CommandError, match="already exists"):
        bootstrap.up()
    assert not identity_path(cfg.state_dir).exists()
    assert not any(a[:3] == ["kind", "create", "cluster"] for a in calls)


def test_the_identity_write_is_atomic(tmp_path) -> None:
    """A temporary file renamed over: a reader sees the old record or the new one."""
    kind_identity._durable_write(tmp_path / "x.json", "old\n")
    kind_identity._durable_write(tmp_path / "x.json", "new\n")
    assert (tmp_path / "x.json").read_text() == "new\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.json"]  # no temporary left
    src = inspect.getsource(kind_identity._durable_write)
    assert "os.replace" in src and "os.fsync" in src


# --- 14: ordering. Ownership evidence is never erased before a confirmed delete ------------------


def test_a_failed_delete_keeps_the_ownership_record(cfg) -> None:
    _pin(cfg)
    cfg.kubeconfig_path.write_text("admin")
    kind = Kind(cfg, delete_fails=True)
    with pytest.raises(CommandError, match="docker daemon not responding"):
        bootstrap.down(tool=kind, docker=docker())
    assert json.loads(identity_path(cfg.state_dir).read_text()) == IDENTITY
    assert cfg.kubeconfig_path.exists()  # nothing local was cleaned up either


def test_a_delete_that_leaves_the_cluster_listed_keeps_the_record(cfg) -> None:
    _pin(cfg)
    kind = Kind(cfg, delete_is_a_lie=True)
    with pytest.raises(CommandError, match="still listed"):
        bootstrap.down(tool=kind, docker=docker())
    assert identity_path(cfg.state_dir).exists()


def test_the_record_is_removed_only_after_the_post_delete_check(cfg) -> None:
    src = inspect.getsource(bootstrap.down)
    delete = src.index("tool(args)")
    confirm = src.index('tool(["kind", "get", "clusters"])', delete)
    unlink = src.index("record.unlink(")
    assert delete < confirm < unlink
    assert src.count("record.unlink(") == 1  # one removal, after both branches


# --- 15: teardown's ownership check needs no Kubernetes at all -----------------------------------


def test_teardown_needs_no_kubernetes_client_kubeconfig_or_api_endpoint(cfg, monkeypatch) -> None:
    import safeops.cluster
    from safeops.environment.node import NodeInspector

    def forbidden(*a, **k):
        raise AssertionError("teardown touched Kubernetes")

    monkeypatch.setattr(safeops.cluster.Kubectl, "__init__", forbidden)
    monkeypatch.setattr(bootstrap, "Kubectl", forbidden)
    monkeypatch.setattr(NodeInspector, "verify_identity", forbidden)
    monkeypatch.setattr(NodeInspector, "__init__", forbidden)
    cfg.harness_kubeconfig_path.write_text("not a kubeconfig")  # never read as evidence
    _pin(cfg)
    d = docker()
    kind = Kind(cfg)  # kubectl or helm through it raises
    bootstrap.down(tool=kind, docker=d)
    assert kind.deleted
    assert all(a[:2] == ["docker", "inspect"] for a in d.calls)  # host-side only
    assert {a[0] for a in kind.calls} == {"kind"}


def test_the_identity_module_imports_only_the_standard_library() -> None:
    tree = ast.parse(inspect.getsource(kind_identity))
    imported = {
        (n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}
    src = inspect.getsource(kind_identity).split('"""', 2)[2]  # code, past the docstring
    for word in ("kubectl", "Kubectl", "kubeconfig", "6443", "api_server"):
        assert word not in src, word
