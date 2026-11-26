"""Milestone 8B-2, offline: the safeops-executor manifest, its RBAC expectations, the strict
`can-i` reading, bootstrap's credential handling, and the audit trail's repository policy
(decision 0025 D§11.8, D§14, M8B-2 amendment).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from safeops import bootstrap, manifests
from safeops.cluster import EXECUTOR_RESOURCES, CommandError, load_cluster_config
from tests.m8_fixtures import TOPOLOGY

CFG = load_cluster_config()
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = manifests.MANIFEST_DIR / "system" / "safeops-executor.yaml"
# D§11.1 E3 / D§14, written out: the whitelist the Role must equal.
WHITELIST = {
    "Deployment": ("gateway", "inventory", "orders", "payment"),
    "Service": ("gateway-svc", "inventory-svc", "orders-svc", "payment-svc", "postgres-svc"),
    "ConfigMap": ("db-pool-config", "orders-config"),
}


def _objects() -> dict:
    values = manifests.template_values(CFG, manifests.image_refs())
    return {
        (o["kind"], o["metadata"]["name"], o["metadata"].get("namespace")): o
        for o in manifests.load_objects(MANIFEST, values)
    }


# --- the manifest ---------------------------------------------------------------------------


def test_the_executor_manifest_is_one_namespaced_sa_role_and_binding() -> None:
    assert set(_objects()) == {
        ("ServiceAccount", "safeops-executor", CFG.system_namespace),
        ("Role", "safeops-executor", CFG.namespace),
        ("RoleBinding", "safeops-executor", CFG.namespace),
    }


def test_the_role_is_patch_only_on_exactly_the_whitelist() -> None:
    role = _objects()[("Role", "safeops-executor", CFG.namespace)]
    groups = {"Deployment": "apps", "Service": "", "ConfigMap": ""}
    plural = {kind: res.split(".")[0] for kind, res in EXECUTOR_RESOURCES.items()}
    assert role["rules"] == [
        {
            "apiGroups": [groups[kind]],
            "resources": [plural[kind]],
            "resourceNames": list(WHITELIST[kind]),
            "verbs": ["patch"],
        }
        for kind in ("Deployment", "Service", "ConfigMap")
    ]


def test_the_role_names_equal_the_ownership_map_whitelist() -> None:
    assert bootstrap.executor_whitelist(TOPOLOGY) == WHITELIST
    role = _objects()[("Role", "safeops-executor", CFG.namespace)]
    by_resource = {rule["resources"][0]: tuple(rule["resourceNames"]) for rule in role["rules"]}
    assert by_resource == {
        EXECUTOR_RESOURCES[k].split(".")[0]: names for k, names in WHITELIST.items()
    }


def test_the_role_grants_nothing_outside_the_three_families() -> None:
    role = _objects()[("Role", "safeops-executor", CFG.namespace)]
    text = json.dumps(role)
    assert "*" not in text
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    verbs = {v for rule in role["rules"] for v in rule["verbs"]}
    assert resources == {"deployments", "services", "configmaps"}
    assert verbs == {"patch"}
    for absent in ("statefulsets", "persistentvolumeclaims", "secrets", "pods", "replicasets"):
        assert absent not in resources
    assert not any("/" in r for r in resources)  # no subresource (scale, status)
    for verb in ("get", "list", "watch", "update", "create", "delete", "deletecollection"):
        assert verb not in verbs


def test_the_binding_binds_only_the_executor_service_account() -> None:
    binding = _objects()[("RoleBinding", "safeops-executor", CFG.namespace)]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "safeops-executor", "namespace": CFG.system_namespace}
    ]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "safeops-executor",
    }


def test_the_recorded_bootstrap_scope_is_unchanged_by_the_executor() -> None:
    recorded = yaml.safe_load((REPO_ROOT / "config" / "bootstrap_scope.yaml").read_text())
    ours = bootstrap._safeops_objects(CFG, bootstrap._UNBUILT)
    flat = [o for group in ours.values() for o in group]
    assert bootstrap._cluster_scoped(flat, None) == recorded["safeops_manifests"]["cluster_scoped"]
    assert bootstrap._cluster_scoped(list(_objects().values()), None) == []


def test_the_reset_inventory_accepts_the_executor_rbac_in_the_testbed() -> None:
    from safeops.environment.manager import EnvironmentManager

    allow = EnvironmentManager._rbac_allowlist(type("M", (), {"cfg": CFG})())
    assert {("Role", "safeops-executor"), ("RoleBinding", "safeops-executor")} <= allow


# --- the RBAC expectations and the strict can-i reading ---------------------------------------


def test_the_executor_expectations_allow_exactly_patch_on_the_whitelist() -> None:
    expect = bootstrap.executor_rbac_expectations(CFG, TOPOLOGY)
    allowed = {(verb, res, sub, ns) for verb, res, sub, ns, want in expect if want}
    assert allowed == {
        ("patch", f"{EXECUTOR_RESOURCES[kind]}/{name}", None, CFG.namespace)
        for kind, names in WHITELIST.items()
        for name in names
    }
    denied = {(verb, res, sub) for verb, res, sub, ns, want in expect if not want}
    for kind, names in WHITELIST.items():
        for name in names:
            for verb in ("get", "update", "delete", "create"):
                assert (verb, f"{EXECUTOR_RESOURCES[kind]}/{name}", None) in denied
    assert ("patch", "deployments.apps/orders", "scale") in denied
    assert ("patch", "statefulsets.apps/postgres", None) in denied
    assert ("get", "secrets", None) in denied
    assert ("list", "deployments.apps", None) in denied


def _proc(rc, out, err=""):
    return subprocess.CompletedProcess(["kubectl"], rc, out, err)


def test_can_i_reads_only_an_explicit_yes_or_no() -> None:
    assert bootstrap.can_i_answer(_proc(0, "yes\n")) is True
    assert bootstrap.can_i_answer(_proc(1, "no\n")) is False
    for bad in (
        _proc(1, "yes"),
        _proc(0, "no"),
        _proc(1, ""),
        _proc(0, ""),
        _proc(2, "no"),
        _proc(1, "", "error: the server doesn't have a resource type"),
        _proc(0, "yes\nno"),
        _proc(0, "maybe"),
    ):
        with pytest.raises(CommandError, match="no answer"):
            bootstrap.can_i_answer(bad)


def test_rbac_checks_cover_all_three_identities_through_the_strict_reader(monkeypatch) -> None:
    seen: list[str] = []

    def fake_can_i(cfg, verb, resource, sub, ns, service_account="experiment-controller"):
        seen.append(service_account)
        raise CommandError("kubectl auth can-i gave no answer")

    monkeypatch.setattr(bootstrap, "_can_i", fake_can_i)
    with pytest.raises(CommandError, match="no answer"):  # a failed check never reads as "no"
        bootstrap.rbac_checks(CFG)
    seen.clear()
    monkeypatch.setattr(
        bootstrap,
        "_can_i",
        lambda cfg, v, r, s, n, service_account="experiment-controller": (
            seen.append(service_account) or False
        ),
    )
    bootstrap.rbac_checks(CFG)
    assert {"experiment-controller", "safeops-reader", "safeops-executor"} <= set(seen)


def test_the_admin_impersonation_check_uses_the_strict_reader(monkeypatch) -> None:
    def run(args, **kw):
        assert args[args.index("--as") + 1] == (
            f"system:serviceaccount:{CFG.system_namespace}:safeops-executor"
        )
        return _proc(1, "")  # an error, not a "no"

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(CommandError):
        bootstrap._can_i(CFG, "patch", "services", None, "testbed", "safeops-executor")


# --- bootstrap's executor credential -----------------------------------------------------------


def _jwt(claims: dict) -> str:
    enc = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{enc}.signature"


def test_bootstrap_issues_the_executor_credential_to_its_own_paths(tmp_path) -> None:
    from tests.conftest import scratch_cluster_config

    cfg = scratch_cluster_config(tmp_path)
    cfg.kubeconfig_path.write_text(
        yaml.safe_dump({"clusters": [{"name": "kind-safeops", "cluster": {"server": "https://x"}}]})
    )

    class FakeKubectl:
        def __init__(self):
            self.calls = []

        def create_token(self, sa, ns, duration):
            self.calls.append((sa, ns))
            return _jwt({"iat": 0, "exp": cfg.harness_session_s})

    k = FakeKubectl()
    meta = bootstrap.issue_executor_credential(cfg, k)
    assert k.calls == [("safeops-executor", CFG.system_namespace)]
    assert meta["service_account"] == f"{CFG.system_namespace}/safeops-executor"
    kc = yaml.safe_load(cfg.executor_kubeconfig_path.read_text())
    assert kc["users"][0]["name"] == "safeops-executor"
    assert oct(cfg.executor_kubeconfig_path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(cfg.executor_token_meta_path.read_text())["expires_at"] == meta["expires_at"]
    assert not cfg.reader_kubeconfig_path.exists() and not cfg.harness_kubeconfig_path.exists()


def test_cluster_down_removes_the_executor_credential(tmp_path, monkeypatch) -> None:
    from tests.conftest import scratch_cluster_config

    cfg = scratch_cluster_config(tmp_path)
    monkeypatch.setattr(bootstrap, "load_cluster_config", lambda: cfg)
    for path in (cfg.executor_kubeconfig_path, cfg.executor_token_meta_path):
        path.write_text("x")
    bootstrap.down(tool=lambda args, **kw: "", docker=None)  # no cluster: nothing to verify
    assert not cfg.executor_kubeconfig_path.exists()
    assert not cfg.executor_token_meta_path.exists()


# --- repository policy: the audit trail is never committed --------------------------------------

_spec = importlib.util.spec_from_file_location(
    "check_repo_policy_m8b2", REPO_ROOT / "scripts/check_repo_policy.py"
)
policy = importlib.util.module_from_spec(_spec)
sys.modules["check_repo_policy_m8b2"] = policy
_spec.loader.exec_module(policy)


@pytest.mark.parametrize(
    "path",
    [
        "data/executor_audit/00000000-0000-4000-8000-00000000000a.jsonl",
        "data/executor_audit/nested/x.jsonl",
        "data/executor_audit",
    ],
)
def test_a_tracked_audit_file_fails_the_policy(path) -> None:
    assert policy.forbidden_reason(path) is not None
    assert policy.hygiene_problems([path])


def test_the_audit_root_is_gitignored() -> None:
    probe = "data/executor_audit/00000000-0000-4000-8000-00000000000a.jsonl"
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "--no-index", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0 and out.stdout.strip() == probe


def test_a_force_added_audit_file_fails_the_tracked_tree_check(tmp_path, monkeypatch) -> None:
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(k, raising=False)
    root = tmp_path / "repo"
    (root / "data" / "executor_audit").mkdir(parents=True)
    (root / ".gitignore").write_text("data/executor_audit/\n")
    (root / "data" / "executor_audit" / "run.jsonl").write_text("{}\n")

    def git(args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        ).stdout

    git(["init", "-q", "-b", "main"])
    git(["add", "-f", "data/executor_audit/run.jsonl", ".gitignore"])
    git(["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x"])
    problems = policy.hygiene_problems(policy.tracked_paths(git))
    assert problems and "data/executor_audit/" in problems[0]
