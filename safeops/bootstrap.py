"""Cluster bootstrap: `make cluster-up`, `make cluster-down`, `make bootstrap-scope`
(decisions 0006, 0007, 0009, 0010).

This is the only module allowed to use Phase.BOOTSTRAP, which tests/test_cluster_guard.py
enforces. Nothing here is reachable from RUN_SEQUENCE.

    python -m safeops.bootstrap up | down | render-scope
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from safeops import manifests
from safeops.cluster import (
    REPO_ROOT,
    ClusterConfig,
    CommandError,
    CredentialError,
    GuardError,
    Kubectl,
    Phase,
    WorkloadProxy,
    decode_token_times,
    load_cluster_config,
)
from safeops.topology import load_topology, ownership_report

SCOPE_PATH = REPO_ROOT / "config" / "bootstrap_scope.yaml"
KIND_CONFIG = REPO_ROOT / "kind" / "cluster.yaml"
HELM_VALUES = REPO_ROOT / "manifests" / "helm"

# Built-in cluster-scoped kinds, for classifying rendered manifests offline. Bootstrap
# cross-checks this against the live API (`kubectl api-resources --namespaced=false`)
# before installing anything, so a kind missing from this list is caught, not guessed.
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

# Images are irrelevant to scope rendering; any valid reference will do.
_UNBUILT = {"testbed": "safeops-testbed:unbuilt", "loadgen": "safeops-loadgen:unbuilt"}
ROLLOUT_TIMEOUT = "300s"
TARGETS_TIMEOUT_S = 180


def _say(msg: str) -> None:
    print(f"[cluster-up] {msg}", flush=True)


def _tool(args: list[str], *, stdin: str | None = None, timeout: float = 900) -> str:
    proc = subprocess.run(
        args, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        lines = [
            ln.strip()
            for ln in (proc.stderr or proc.stdout or "").splitlines()
            if ln.strip() and not ln.startswith("View build details")
        ]
        errors = [ln for ln in lines if "error" in ln.lower()]
        detail = (errors or lines or [f"exit {proc.returncode}"])[-1]
        raise CommandError(f"{args[0]} {args[1] if len(args) > 1 else ''} failed: {detail}")
    return proc.stdout


# --- charts and scope ----------------------------------------------------------------


def _charts(cfg: ClusterConfig) -> dict[str, dict]:
    return {
        "chaos-mesh": {
            "chart": "chaos-mesh",
            "repo": cfg.chaos_mesh_repo,
            "version": cfg.chaos_mesh_version,
            "namespace": cfg.chaos_namespace,
            "values": HELM_VALUES / "chaos-mesh-values.yaml",
            "crds": True,
        },
        "prometheus": {
            "chart": "prometheus",
            "repo": cfg.prometheus_chart_repo,
            "version": cfg.prometheus_chart_version,
            "namespace": cfg.monitoring_namespace,
            "values": HELM_VALUES / "prometheus-values.yaml",
            "crds": False,
        },
    }


def _render_values(cfg: ClusterConfig, release: str, spec: dict) -> Path:
    out = cfg.kubeconfig_path.parent / "render" / f"{release}-values.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    values = manifests.template_values(cfg, _UNBUILT)
    out.write_text(manifests.render(spec["values"].read_text(), values))
    return out


def _helm_template(cfg: ClusterConfig, release: str, spec: dict) -> list[dict]:
    import yaml

    args = [
        "helm",
        "template",
        release,
        spec["chart"],
        "--repo",
        spec["repo"],
        "--version",
        spec["version"],
        "--namespace",
        spec["namespace"],
        "-f",
        str(_render_values(cfg, release, spec)),
    ]
    if spec["crds"]:
        args.append("--include-crds")
    return [d for d in yaml.safe_load_all(_tool(args)) if d]


def _cluster_scoped(objects: list[dict], release_namespace: str | None) -> list[dict]:
    """Cluster-scoped (kind, name) pairs. Refuses namespaced objects outside the release ns."""
    found = set()
    for obj in objects:
        kind, meta = obj["kind"], obj.get("metadata", {})
        if kind in CLUSTER_SCOPED_KINDS:
            found.add((kind, meta["name"]))
        elif release_namespace is not None and meta.get("namespace") not in (
            None,
            release_namespace,
        ):
            raise GuardError(
                f"{kind}/{meta['name']} targets namespace {meta.get('namespace')!r},"
                f" outside its release namespace {release_namespace!r}"
            )
    return [{"kind": k, "name": n} for k, n in sorted(found)]


def _safeops_objects(cfg: ClusterConfig, images: dict[str, str]) -> dict[str, list[dict]]:
    values = manifests.template_values(cfg, images)
    return {
        group: [
            o for f in manifests.manifest_files(group) for o in manifests.load_objects(f, values)
        ]
        for group in ("system", "testbed", "workload")
    }


def compute_scope(cfg: ClusterConfig) -> dict:
    charts = {}
    for release, spec in _charts(cfg).items():
        charts[release] = {
            "chart": spec["chart"],
            "version": spec["version"],
            "namespace": spec["namespace"],
            "cluster_scoped": _cluster_scoped(
                _helm_template(cfg, release, spec), spec["namespace"]
            ),
        }
    ours = _safeops_objects(cfg, _UNBUILT)
    safeops = {"cluster_scoped": _cluster_scoped([o for g in ours.values() for o in g], None)}
    return {"charts": charts, "safeops_manifests": safeops}


def _scope_header() -> str:
    return (
        "# Bootstrap scope (decision 0006): the exact cluster-scoped objects bootstrap may\n"
        "# create. Generated by `make bootstrap-scope` from the pinned charts and SafeOps\n"
        "# manifests. Review every change. cluster-up re-renders and refuses on any\n"
        "# difference.\n"
    )


def render_scope() -> None:
    import yaml

    cfg = load_cluster_config()
    SCOPE_PATH.write_text(_scope_header() + yaml.safe_dump(compute_scope(cfg), sort_keys=False))
    print(f"wrote {SCOPE_PATH.relative_to(REPO_ROOT)}")


def verified_scope(cfg: ClusterConfig) -> frozenset[tuple[str, str]]:
    import yaml

    if not SCOPE_PATH.is_file():
        raise GuardError("config/bootstrap_scope.yaml missing; run `make bootstrap-scope`")
    recorded = yaml.safe_load(SCOPE_PATH.read_text())
    current = compute_scope(cfg)
    if recorded != current:
        raise GuardError(
            "rendered bootstrap scope differs from config/bootstrap_scope.yaml;"
            " re-run `make bootstrap-scope`, review the diff, and amend 0006"
        )
    pairs = {
        (o["kind"], o["name"]) for c in current["charts"].values() for o in c["cluster_scoped"]
    }
    pairs |= {(o["kind"], o["name"]) for o in current["safeops_manifests"]["cluster_scoped"]}
    return frozenset(pairs)


# --- images --------------------------------------------------------------------------


def build_images(cfg: ClusterConfig) -> dict[str, dict]:
    """Reproducible OCI archives: identical inputs give a byte-identical image (decision 0007).

    The archive is loaded straight into kind, so the digest pods report is the manifest
    digest buildx printed here. Docker's own image store isn't involved, and neither are
    its unpack and attestation defaults.
    """
    out_dir = cfg.kubeconfig_path.parent / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for image, ref in manifests.image_refs().items():
        archive = out_dir / f"{image}.tar"
        printed = _tool(
            [
                "docker",
                "buildx",
                "build",
                "--quiet",
                "--provenance=false",
                "--sbom=false",
                "--build-arg",
                "SOURCE_DATE_EPOCH=0",
                "--output",
                f"type=oci,dest={archive},name=docker.io/library/{ref},rewrite-timestamp=true",
                "--target",
                image,
                str(manifests.TESTBED_SRC),
            ]
        )
        printed_ids = [
            ln.strip() for ln in printed.splitlines() if ln.strip().startswith("sha256:")
        ]
        if not printed_ids:
            raise CommandError(f"buildx printed no digest for {ref}")
        out[image] = {
            "ref": ref,
            "archive": str(archive),
            **_archive_digests(archive),
            "config_digest": printed_ids[-1],
        }
    return out


def _archive_digests(archive: Path) -> dict[str, str]:
    """Digests of a single-image OCI archive.

    index_digest is what running pods report in status.containerStatuses[].imageID once
    the archive is loaded into kind, so it is the provenance digest (decision 0007).
    """
    import hashlib
    import tarfile

    with tarfile.open(archive) as tar:
        member = tar.extractfile("index.json")
        if member is None:
            raise CommandError(f"{archive.name} has no index.json")
        raw = member.read()
    manifests_ = json.loads(raw)["manifests"]
    if len(manifests_) != 1:
        raise CommandError(f"{archive.name}: expected one image manifest, got {len(manifests_)}")
    return {
        "index_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "manifest_digest": manifests_[0]["digest"],
    }


# --- harness credential --------------------------------------------------------------


def issue_harness_credential(cfg: ClusterConfig, k: Kubectl) -> dict:
    """Time-limited TokenRequest token for experiment-controller (decision 0009)."""
    import yaml

    token = k.create_token("experiment-controller", cfg.system_namespace, cfg.harness_session_s)
    iat, exp = decode_token_times(token)
    actual = exp - iat
    if actual < cfg.harness_session_s:
        raise CredentialError(
            f"API server issued a {actual}s token; the intended session is"
            f" {cfg.harness_session_s}s (harness_session_s). Refusing to continue."
        )
    admin = yaml.safe_load(cfg.kubeconfig_path.read_text())
    cluster = next(c for c in admin["clusters"] if c["name"] == cfg.kube_context)
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": cfg.kube_context, "cluster": cluster["cluster"]}],
        "users": [{"name": "experiment-controller", "user": {"token": token}}],
        "contexts": [
            {
                "name": cfg.kube_context,
                "context": {"cluster": cfg.kube_context, "user": "experiment-controller"},
            }
        ],
    }
    path = cfg.harness_kubeconfig_path
    path.write_text(yaml.safe_dump(kubeconfig))
    os.chmod(path, 0o600)
    meta = {
        "service_account": f"{cfg.system_namespace}/experiment-controller",
        "kind": "time-limited TokenRequest ServiceAccount token (no boundObjectRef)",
        "requested_s": cfg.harness_session_s,
        "issued_at": iat,
        "expires_at": exp,
        "actual_s": actual,
    }
    cfg.harness_token_meta_path.write_text(json.dumps(meta, indent=2))
    return meta


# --- health checks -------------------------------------------------------------------


def _can_i(cfg: ClusterConfig, verb: str, resource: str, sub: str | None, namespace: str) -> bool:
    sa = f"system:serviceaccount:{cfg.system_namespace}:experiment-controller"
    proc = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(cfg.kubeconfig_path),
            "--context",
            cfg.kube_context,
            "auth",
            "can-i",
            verb,
            resource,
            *([f"--subresource={sub}"] if sub else []),
            "-n",
            namespace,
            "--as",
            sa,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.stdout.strip() == "yes"


def rbac_checks(cfg: ClusterConfig) -> list[str]:
    """experiment-controller may proxy GET/PUT to exactly the workload generator."""
    wl, svc = cfg.workload_namespace, cfg.workload_service
    expect = [  # (verb, resource, subresource, namespace, expected)
        ("get", f"services/{svc}", "proxy", wl, True),
        ("update", f"services/{svc}", "proxy", wl, True),
        ("create", f"services/{svc}", "proxy", wl, False),
        ("get", "services/some-other-service", "proxy", wl, False),
        ("get", f"services/{svc}", None, wl, False),
        ("get", "services/gateway-svc", "proxy", cfg.namespace, False),
        ("get", "secrets", None, cfg.namespace, False),
        ("get", "pods", None, cfg.namespace, False),  # testbed rights arrive at Milestone 3
    ]
    failures = []
    for verb, resource, sub, ns, want in expect:
        got = _can_i(cfg, verb, resource, sub, ns)
        if got != want:
            what = f"{resource}" + (f" --subresource={sub}" if sub else "")
            failures.append(f"can-i {verb} {what} -n {ns}: expected {want}, got {got}")
    return failures


def wait_rollouts(k: Kubectl, namespaces: tuple[str, ...]) -> None:
    for ns in namespaces:
        for kind in ("deployment", "statefulset"):
            names = k.read("get", kind, "-n", ns, "-o", "name").split()
            for name in names:
                k.read(
                    "rollout", "status", name, "-n", ns, f"--timeout={ROLLOUT_TIMEOUT}", timeout=330
                )


def wait_targets(cfg: ClusterConfig, k: Kubectl, expected: dict[str, int]) -> dict[str, int]:
    path = (
        f"/api/v1/namespaces/{cfg.monitoring_namespace}/services/"
        f"{cfg.prometheus_service}:{cfg.prometheus_service_port}/proxy/api/v1/targets?state=active"
    )
    deadline = time.monotonic() + TARGETS_TIMEOUT_S
    up: dict[str, int] = {}
    while True:
        data = json.loads(k.raw("GET", path))
        up = {}
        for t in data["data"]["activeTargets"]:
            if t.get("health") == "up":
                job = t["labels"].get("job", "?")
                up[job] = up.get(job, 0) + 1
        if all(up.get(job, 0) >= n for job, n in expected.items()):
            return up
        if time.monotonic() > deadline:
            raise CommandError(
                f"Prometheus targets not all up after {TARGETS_TIMEOUT_S}s:"
                f" expected {expected}, up {up}"
            )
        time.sleep(5)


def ownership_check(cfg: ClusterConfig, k: Kubectl) -> None:
    from safeops.topology import PROJECTED_KINDS

    kinds = ",".join(k_.lower() for k_ in PROJECTED_KINDS)
    items = k.get_json(kinds, "-n", cfg.namespace)["items"]
    report = ownership_report(load_topology(), items)
    if report:
        raise CommandError("ownership map does not resolve the testbed: " + "; ".join(report))


def pod_image_digests(cfg: ClusterConfig, k: Kubectl) -> dict[str, set[str]]:
    seen: dict[str, set[str]] = {}
    for ns in (cfg.namespace, cfg.workload_namespace):
        for pod in k.get_json("pods", "-n", ns)["items"]:
            for cs in pod.get("status", {}).get("containerStatuses", []):
                seen.setdefault(cs["image"], set()).add(cs.get("imageID", ""))
    return seen


def memory_report(cfg: ClusterConfig) -> str:
    node = f"{cfg.cluster_name}-control-plane"
    out = _tool(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}} ({{.MemPerc}})", node]
    )
    return out.strip()


# --- up / down -----------------------------------------------------------------------


def _preflight(cfg: ClusterConfig) -> None:
    for tool in ("docker", "kind", "kubectl", "helm"):
        if shutil.which(tool) is None:
            raise CommandError(f"{tool} is not on PATH")
    _tool(["docker", "info", "--format", "{{.ServerVersion}}"])
    if cfg.cluster_name in _tool(["kind", "get", "clusters"]).split():
        raise CommandError(
            f"kind cluster {cfg.cluster_name!r} already exists; run `make cluster-down` first"
        )


def up() -> None:
    cfg = load_cluster_config()
    _preflight(cfg)
    _say("rendering pinned charts and checking the recorded bootstrap scope")
    scope = verified_scope(cfg)

    _say("building images (reproducible)")
    images = build_images(cfg)
    refs = {name: info["ref"] for name, info in images.items()}

    _say(f"creating kind cluster {cfg.cluster_name} ({cfg.kind_node_image.split('@')[0]})")
    cfg.kubeconfig_path.parent.mkdir(parents=True, exist_ok=True)
    _tool(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            cfg.cluster_name,
            "--image",
            cfg.kind_node_image,
            "--config",
            str(KIND_CONFIG),
            "--kubeconfig",
            str(cfg.kubeconfig_path),
            "--wait",
            "180s",
        ]
    )
    os.chmod(cfg.kubeconfig_path, 0o600)
    k = Kubectl(cfg, Phase.BOOTSTRAP)

    live = {
        line.split()[-1]
        for line in k.read("api-resources", "--namespaced=false", "--no-headers").splitlines()
        if line.strip()
    }
    unknown = {kind for kind, _ in scope} - live
    if unknown:
        raise GuardError(
            f"recorded cluster-scoped kinds unknown to the API server: {sorted(unknown)}"
        )

    _say("loading images into kind")
    for info in images.values():
        _tool(["kind", "load", "image-archive", info["archive"], "--name", cfg.cluster_name])

    ours = _safeops_objects(cfg, refs)
    _say("creating namespaces and the experiment-controller identity")
    namespaces = [o for o in ours["system"] if o["kind"] == "Namespace"]
    identity = [o for o in ours["system"] if o["kind"] != "Namespace"]
    for batch in (namespaces, identity):  # namespaces must exist before what lives in them
        k.apply(batch, cluster_scope=scope, cluster_scoped_kinds=CLUSTER_SCOPED_KINDS)

    for release, spec in _charts(cfg).items():
        _say(f"installing {release} {spec['version']} into {spec['namespace']}")
        args = [
            "helm",
            "install",
            release,
            spec["chart"],
            "--repo",
            spec["repo"],
            "--version",
            spec["version"],
            "--namespace",
            spec["namespace"],
            "--kubeconfig",
            str(cfg.kubeconfig_path),
            "--kube-context",
            cfg.kube_context,
            "-f",
            str(_render_values(cfg, release, spec)),
            "--wait",
            "--timeout",
            "10m",
        ]
        _tool(args)

    _say("deploying the testbed and the workload generator")
    password = secrets.token_urlsafe(24)
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": "postgres-credentials",
            "namespace": cfg.namespace,
            "labels": {"app": "postgres"},
        },
        "stringData": {"password": password},
    }
    k.apply([secret] + ours["testbed"] + ours["workload"])
    del password, secret
    wait_rollouts(
        k, (cfg.namespace, cfg.workload_namespace, cfg.monitoring_namespace, cfg.chaos_namespace)
    )

    _say("issuing the harness credential")
    meta = issue_harness_credential(cfg, k)

    _say("health checks")
    ownership_check(cfg, k)
    testbed_pods = sum(o["spec"]["replicas"] for o in ours["testbed"] if o["kind"] == "Deployment")
    up_targets = wait_targets(
        cfg,
        k,
        {
            "prometheus": 1,
            "safeops-testbed-pods": testbed_pods,
            "safeops-workload-generator": 1,
            "safeops-cadvisor": 1,
        },
    )
    failures = rbac_checks(cfg)
    if failures:
        raise GuardError("RBAC checks failed: " + "; ".join(failures))
    state = WorkloadProxy(cfg).stats()  # through the harness credential, not admin
    digests = pod_image_digests(cfg, k)
    for info in images.values():
        ids = digests.get(f"docker.io/library/{info['ref']}", set()) | digests.get(
            info["ref"], set()
        )
        if not ids or not all(i.endswith("@" + info["index_digest"]) for i in ids):
            raise CommandError(
                f"running pods for {info['ref']} report {sorted(ids)}, not the built"
                f" index digest {info['index_digest']}"
            )
    (cfg.kubeconfig_path.parent / "images.json").write_text(json.dumps(images, indent=2))

    _say("healthy")
    print(
        "  images:      " + ", ".join(f"{i['ref']} = {i['index_digest']}" for i in images.values())
    )
    print(f"  targets up:  {dict(sorted(up_targets.items()))}")
    print("  ownership:   every testbed object resolves to config/topology.yaml, and back")
    print(
        f"  harness:     experiment-controller token, {meta['actual_s']}s lifetime"
        f" (expires {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(meta['expires_at']))})"
    )
    print(f"  rbac:        proxy to {cfg.workload_service} allowed; everything else denied")
    running = state.get("running")
    print(f"  workload:    generator reachable via the harness path (running={running})")
    print(f"  node memory: {memory_report(cfg)}")


def down() -> None:
    cfg = load_cluster_config()
    if cfg.cluster_name in _tool(["kind", "get", "clusters"]).split():
        kubeconfig = cfg.kubeconfig_path if cfg.kubeconfig_path.is_file() else None
        args = ["kind", "delete", "cluster", "--name", cfg.cluster_name]
        if kubeconfig is not None:
            args += ["--kubeconfig", str(kubeconfig)]
        _tool(args)
        print(f"deleted kind cluster {cfg.cluster_name}")
    else:
        print(f"kind cluster {cfg.cluster_name} does not exist")
    for path in (
        cfg.kubeconfig_path,
        cfg.harness_kubeconfig_path,
        cfg.harness_token_meta_path,
        cfg.kubeconfig_path.parent / "images.json",
    ):
        path.unlink(missing_ok=True)
    shutil.rmtree(cfg.kubeconfig_path.parent / "images", ignore_errors=True)


def main(argv: list[str]) -> int:
    commands = {"up": up, "down": down, "render-scope": render_scope}
    if len(argv) != 1 or argv[0] not in commands:
        print("usage: python -m safeops.bootstrap up|down|render-scope", file=sys.stderr)
        return 2
    try:
        commands[argv[0]]()
    except (CommandError, CredentialError, GuardError, ValueError) as exc:
        print(f"[cluster-{argv[0]}] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
