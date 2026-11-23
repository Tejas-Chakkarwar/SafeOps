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
from collections.abc import Callable
from pathlib import Path

from safeops import manifests
from safeops.cluster import (
    CLUSTER_SCOPED_KINDS,
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


def pull_charts(cfg: ClusterConfig) -> dict[str, Path]:
    """Download each pinned chart exactly once. Template and install both use this file,
    so what is checked is byte-for-byte what is installed (its sha256 is recorded)."""
    _private_state_dir(cfg)
    out_dir = cfg.kubeconfig_path.parent / "charts"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    pulled = {}
    for release, spec in _charts(cfg).items():
        dest = out_dir / release
        dest.mkdir()
        _tool(
            [
                "helm",
                "pull",
                spec["chart"],
                "--repo",
                spec["repo"],
                "--version",
                spec["version"],
                "-d",
                str(dest),
            ]
        )
        archives = list(dest.glob("*.tgz"))
        if len(archives) != 1:
            raise CommandError(f"helm pull {spec['chart']} produced {len(archives)} archives")
        pulled[release] = archives[0]
    return pulled


def _sha256(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _helm_template(cfg: ClusterConfig, release: str, spec: dict, chart: Path) -> list[dict]:
    import yaml

    args = [
        "helm",
        "template",
        release,
        str(chart),
        "--namespace",
        spec["namespace"],
        "-f",
        str(_render_values(cfg, release, spec)),
    ]
    if spec["crds"]:
        args.append("--include-crds")
    return [d for d in yaml.safe_load_all(_tool(args)) if d]


def _cluster_scoped(
    objects: list[dict],
    release_namespace: str | None,
    cluster_kinds: frozenset[str] = CLUSTER_SCOPED_KINDS,
    namespaced_kinds: frozenset[str] | None = None,
) -> list[dict]:
    """Cluster-scoped (kind, name) pairs among rendered objects.

    Classification: built-in or live cluster-scoped kinds, plus kinds whose CRD in the
    same render says scope: Cluster. With `namespaced_kinds` (live, at cluster-up), a
    kind that is neither known namespaced, known cluster-scoped, nor defined by a CRD in
    the render is refused instead of guessed. Offline (render-scope) there is no live API
    to ask, so cluster-up re-classifies against the live API and refuses on any difference.
    Namespaced objects must target their release namespace.
    """
    crd_scope = {
        o["spec"]["names"]["kind"]: o["spec"].get("scope")
        for o in objects
        if o.get("kind") == "CustomResourceDefinition"
    }
    found = set()
    for obj in objects:
        kind, meta = obj["kind"], obj.get("metadata", {})
        if kind in cluster_kinds or crd_scope.get(kind) == "Cluster":
            found.add((kind, meta["name"]))
            continue
        if (
            namespaced_kinds is not None
            and kind not in namespaced_kinds
            and crd_scope.get(kind) != "Namespaced"
        ):
            raise GuardError(
                f"{kind}/{meta['name']}: kind unknown to the API server;"
                " refusing to guess its scope"
            )
        if release_namespace is not None and meta.get("namespace") not in (None, release_namespace):
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


def compute_scope(
    cfg: ClusterConfig,
    charts: dict[str, Path],
    cluster_kinds: frozenset[str] = CLUSTER_SCOPED_KINDS,
    namespaced_kinds: frozenset[str] | None = None,
) -> dict:
    out = {}
    for release, spec in _charts(cfg).items():
        objects = _helm_template(cfg, release, spec, charts[release])
        out[release] = {
            "chart": spec["chart"],
            "version": spec["version"],
            "chart_sha256": _sha256(charts[release]),
            "namespace": spec["namespace"],
            "cluster_scoped": _cluster_scoped(
                objects, spec["namespace"], cluster_kinds, namespaced_kinds
            ),
        }
    ours = _safeops_objects(cfg, _UNBUILT)
    flat = [o for g in ours.values() for o in g]
    safeops = {"cluster_scoped": _cluster_scoped(flat, None, cluster_kinds, namespaced_kinds)}
    return {"charts": out, "safeops_manifests": safeops}


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
    scope = compute_scope(cfg, pull_charts(cfg))
    SCOPE_PATH.write_text(_scope_header() + yaml.safe_dump(scope, sort_keys=False))
    print(f"wrote {SCOPE_PATH.relative_to(REPO_ROOT)}")


def verified_scope(
    cfg: ClusterConfig,
    charts: dict[str, Path],
    cluster_kinds: frozenset[str] = CLUSTER_SCOPED_KINDS,
    namespaced_kinds: frozenset[str] | None = None,
) -> frozenset[tuple[str, str]]:
    """Re-render and compare with the recorded scope, chart sha256 included.

    Any difference refuses.
    """
    import yaml

    if not SCOPE_PATH.is_file():
        raise GuardError("config/bootstrap_scope.yaml missing; run `make bootstrap-scope`")
    recorded = yaml.safe_load(SCOPE_PATH.read_text())
    current = compute_scope(cfg, charts, cluster_kinds, namespaced_kinds)
    if recorded != current:
        raise GuardError(
            "rendered bootstrap scope (or a chart's sha256) differs from"
            " config/bootstrap_scope.yaml; re-run `make bootstrap-scope`, review the diff,"
            " and amend 0006"
        )
    pairs = {
        (o["kind"], o["name"]) for c in current["charts"].values() for o in c["cluster_scoped"]
    }
    pairs |= {(o["kind"], o["name"]) for o in current["safeops_manifests"]["cluster_scoped"]}
    return frozenset(pairs)


# --- images --------------------------------------------------------------------------


def build_images(cfg: ClusterConfig) -> dict[str, dict]:
    """Reproducible OCI archives: identical inputs give a byte-identical image (decision 0007).

    The archive is loaded straight into kind, so Docker's own image store isn't involved,
    and neither are its unpack and attestation defaults. Running pods report the archive's
    *index* digest (sha256 of its index.json) in imageID. That is the provenance digest,
    and _archive_digests() records it as index_digest. buildx prints the image config
    digest, which is recorded as config_digest for cross-reference (decision 0007).
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


def _write_private(path: Path, text: str) -> None:
    """Create or replace a file that is 0600 from its first byte, never briefly world-readable."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _private_state_dir(cfg: ClusterConfig) -> None:
    state = cfg.kubeconfig_path.parent
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)


def issue_harness_credential(cfg: ClusterConfig, k: Kubectl) -> dict:
    """Time-limited TokenRequest token for experiment-controller (decision 0009)."""
    return _issue_credential(
        cfg, k, "experiment-controller", cfg.harness_kubeconfig_path, cfg.harness_token_meta_path
    )


def issue_reader_credential(cfg: ClusterConfig, k: Kubectl) -> dict:
    """Time-limited TokenRequest token for safeops-reader, the tools' read-only identity
    (design §4; Milestone 6, decision 0023). Same lifetime as the harness token."""
    return _issue_credential(
        cfg, k, "safeops-reader", cfg.reader_kubeconfig_path, cfg.reader_token_meta_path
    )


def _issue_credential(
    cfg: ClusterConfig, k: Kubectl, service_account: str, kubeconfig_path: Path, meta_path: Path
) -> dict:
    import yaml

    token = k.create_token(service_account, cfg.system_namespace, cfg.harness_session_s)
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
        "users": [{"name": service_account, "user": {"token": token}}],
        "contexts": [
            {
                "name": cfg.kube_context,
                "context": {"cluster": cfg.kube_context, "user": service_account},
            }
        ],
    }
    _write_private(kubeconfig_path, yaml.safe_dump(kubeconfig))
    meta = {
        "service_account": f"{cfg.system_namespace}/{service_account}",
        "kind": "time-limited TokenRequest ServiceAccount token (no boundObjectRef)",
        "requested_s": cfg.harness_session_s,
        "issued_at": iat,
        "expires_at": exp,
        "actual_s": actual,
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


# --- health checks -------------------------------------------------------------------


def _can_i(
    cfg: ClusterConfig,
    verb: str,
    resource: str,
    sub: str | None,
    namespace: str,
    service_account: str = "experiment-controller",
) -> bool:
    sa = f"system:serviceaccount:{cfg.system_namespace}:{service_account}"
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
        # Milestone 3 testbed lifecycle rights (decision 0013)
        ("list", "pods", None, cfg.namespace, True),
        ("delete", "deployments.apps", None, cfg.namespace, True),
        ("update", "statefulsets.apps", None, cfg.namespace, True),
        ("delete", "networkchaos.chaos-mesh.org", None, cfg.namespace, True),
        ("create", "networkchaos.chaos-mesh.org", None, cfg.namespace, False),  # Milestone 4
        ("create", "pods", "exec", cfg.namespace, False),
        ("delete", "pods", None, cfg.namespace, False),
        ("list", "deployments.apps", None, cfg.chaos_namespace, False),
        ("list", "deployments.apps", None, "kube-system", False),
        # Milestone 3 review, H1: inventory reads; deletes only for configuration kinds
        ("list", "networkpolicies.networking.k8s.io", None, cfg.namespace, True),
        ("delete", "networkpolicies.networking.k8s.io", None, cfg.namespace, True),
        ("list", "roles.rbac.authorization.k8s.io", None, cfg.namespace, True),
        ("delete", "roles.rbac.authorization.k8s.io", None, cfg.namespace, False),
        ("create", "roles.rbac.authorization.k8s.io", None, cfg.namespace, False),
        # Hardening pass (decision 0021): legacy Endpoints are inventoried and removable
        ("list", "endpoints", None, cfg.namespace, True),
        ("delete", "endpoints", None, cfg.namespace, True),
        ("create", "endpoints", None, cfg.namespace, False),
        ("get", "secrets", None, cfg.system_namespace, False),
    ]
    failures = []
    for verb, resource, sub, ns, want in expect:
        got = _can_i(cfg, verb, resource, sub, ns)
        if got != want:
            what = f"{resource}" + (f" --subresource={sub}" if sub else "")
            failures.append(f"can-i {verb} {what} -n {ns}: expected {want}, got {got}")
    for verb, resource, sub, ns, want in reader_rbac_expectations(cfg):
        got = _can_i(cfg, verb, resource, sub, ns, service_account="safeops-reader")
        if got != want:
            what = f"{resource}" + (f" --subresource={sub}" if sub else "")
            failures.append(f"reader can-i {verb} {what} -n {ns}: expected {want}, got {got}")
    return failures


# The §4 reader surface (Milestone 6, decision 0023): every read it needs, and the denied
# surface around it. Shared by the bootstrap health check and tests/cluster.
READER_READS = (
    "pods",
    "events",
    "services",
    "configmaps",
    "endpointslices.discovery.k8s.io",
    "deployments.apps",
    "replicasets.apps",
    "statefulsets.apps",
)


def reader_rbac_expectations(cfg: ClusterConfig) -> list[tuple]:
    ns, mon = cfg.namespace, cfg.monitoring_namespace
    prom = f"{cfg.prometheus_service}:{cfg.prometheus_service_port}"  # the proxy is by port
    expect: list[tuple] = []
    for resource in READER_READS:
        expect += [(verb, resource, None, ns, True) for verb in ("get", "list", "watch")]
        expect += [
            (verb, resource, None, ns, False)
            for verb in ("create", "update", "patch", "delete", "deletecollection")
        ]
    expect += [
        # pods/log is get only (§4); nothing else on any pod subresource.
        ("get", "pods", "log", ns, True),
        ("create", "pods", "log", ns, False),
        ("create", "pods", "exec", ns, False),
        ("get", "pods", "exec", ns, False),
        ("create", "pods", "attach", ns, False),
        ("create", "pods", "portforward", ns, False),
        ("create", "pods", "eviction", ns, False),
        ("update", "deployments.apps", "scale", ns, False),
        ("patch", "deployments.apps", "scale", ns, False),
        ("get", "services", "proxy", ns, False),
        ("get", "pods", "proxy", ns, False),
        ("get", "secrets", None, ns, False),
        ("list", "secrets", None, ns, False),
        ("list", "persistentvolumeclaims", None, ns, False),
        ("list", "networkchaos.chaos-mesh.org", None, ns, False),
        ("list", "podchaos.chaos-mesh.org", None, ns, False),
        ("list", "roles.rbac.authorization.k8s.io", None, ns, False),
        ("create", "serviceaccounts", "token", ns, False),
        # Prometheus: GET through its Service proxy only.
        ("get", f"services/{prom}", "proxy", mon, True),
        ("create", f"services/{prom}", "proxy", mon, False),
        ("update", f"services/{prom}", "proxy", mon, False),
        ("delete", f"services/{prom}", "proxy", mon, False),
        ("get", "services/some-other-service", "proxy", mon, False),
        ("get", f"services/{cfg.prometheus_service}", "proxy", mon, False),  # only by port
        ("get", f"services/{prom}", None, mon, False),
        ("list", "pods", None, mon, False),
        # The workload namespace and its proxy: never (decision 0009).
        ("get", f"services/{cfg.workload_service}", "proxy", cfg.workload_namespace, False),
        ("update", f"services/{cfg.workload_service}", "proxy", cfg.workload_namespace, False),
        ("list", "pods", None, cfg.workload_namespace, False),
        ("get", "pods", "log", cfg.workload_namespace, False),
        ("list", "events", None, cfg.workload_namespace, False),
        # Every other namespace, and cluster scope.
        ("get", "secrets", None, cfg.system_namespace, False),
        ("list", "pods", None, cfg.chaos_namespace, False),
        ("list", "pods", None, "kube-system", False),
        ("list", "events", None, "default", False),
        ("list", "nodes", None, ns, False),
        ("list", "namespaces", None, ns, False),
    ]
    return expect


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


def pod_image_digests_all(cfg: ClusterConfig, k: Kubectl) -> dict[str, list[str]]:
    """image reference -> sorted imageIDs, across every SafeOps namespace."""
    seen: dict[str, set[str]] = {}
    for ns in cfg.bootstrap_namespaces:
        for pod in k.get_json("pods", "-n", ns)["items"]:
            statuses = pod.get("status", {})
            for cs in statuses.get("containerStatuses", []) + statuses.get(
                "initContainerStatuses", []
            ):
                seen.setdefault(cs["image"], set()).add(cs.get("imageID", ""))
    return {image: sorted(ids) for image, ids in sorted(seen.items())}


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
    _say("pulling pinned charts once and checking the recorded bootstrap scope")
    charts = pull_charts(cfg)
    verified_scope(cfg, charts)  # offline classification, before anything is created

    _say("building images (reproducible)")
    images = build_images(cfg)
    refs = {name: info["ref"] for name, info in images.items()}

    _say(f"creating kind cluster {cfg.cluster_name} ({cfg.kind_node_image.split('@')[0]})")
    _private_state_dir(cfg)
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
    # Pin the exact instance first, before any other step, so a later bootstrap failure
    # still leaves the proof teardown needs (decision 0022). _preflight refused a
    # same-name cluster, so this is the one kind just created: never an adopted one.
    from safeops.kind_identity import KindIdentityError, record_identity

    try:
        record_identity(cfg.state_dir, cfg.cluster_name)
    except (KindIdentityError, OSError) as exc:
        raise CommandError(f"could not record the new cluster's exact identity: {exc}") from exc
    os.chmod(cfg.kubeconfig_path, 0o600)
    k = Kubectl(cfg, Phase.BOOTSTRAP)

    def _kinds(namespaced: bool) -> frozenset[str]:
        out = k.read(
            "api-resources", f"--namespaced={'true' if namespaced else 'false'}", "--no-headers"
        )
        return frozenset(line.split()[-1] for line in out.splitlines() if line.strip())

    live_cluster, live_namespaced = _kinds(False), _kinds(True)
    # Re-classify every rendered object against the live API: a cluster-scoped kind the
    # built-in list doesn't know would change the scope here, and the comparison refuses.
    scope = verified_scope(cfg, charts, CLUSTER_SCOPED_KINDS | live_cluster, live_namespaced)
    unknown = {kind for kind, _ in scope} - live_cluster
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
        k.apply(batch, cluster_scope=scope)

    for release, spec in _charts(cfg).items():
        _say(f"installing {release} {spec['version']} into {spec['namespace']}")
        args = [
            "helm",
            "install",
            release,
            str(charts[release]),  # the exact archive whose sha256 was just verified
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

    _say("issuing the harness and reader credentials")
    meta = issue_harness_credential(cfg, k)
    issue_reader_credential(cfg, k)

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
    # Every running image, third-party included, by the digest pods report (review P1).
    # Chart images are referenced by tag, so this is what records what actually ran.
    running_images = pod_image_digests_all(cfg, k)
    record = {"built": images, "running": running_images}
    (cfg.kubeconfig_path.parent / "images.json").write_text(json.dumps(record, indent=2))

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
    print("  reader:      safeops-reader reads testbed (§4) and queries Prometheus; never writes")
    running = state.get("running")
    print(f"  workload:    generator reachable via the harness path (running={running})")
    print(
        f"  running:     {len(running_images)} images recorded with digests in .safeops/images.json"
    )
    print(f"  node memory: {memory_report(cfg)}")


def down(
    tool: Callable[..., str] | None = None,
    docker: Callable | None = None,
    *,
    force_unverified_ownership: bool = False,
) -> None:
    """Delete the SafeOps kind cluster, only once it proves to be the exact instance this
    state directory created (decision 0022): the kind labels on its control-plane container
    match, and so does its full container ID, recorded by cluster-up. The proof is Docker,
    never Kubernetes: a broken cluster must still go (decision 0021).

    Ordering: verify, delete, confirm it's gone, and only then remove the ownership record.
    A failed delete keeps the record, so the next attempt can still prove ownership.

    `force_unverified_ownership` skips only the container-ID match. main() sets it from the
    explicit operator flag; nothing else does (no environment variable, no config)."""
    from safeops.kind_identity import KindIdentityError, identity_path, verify_owned

    tool = tool or _tool
    cfg = load_cluster_config()
    record = identity_path(cfg.state_dir)
    if cfg.cluster_name in tool(["kind", "get", "clusters"]).split():
        try:
            verify_owned(
                cfg.state_dir,
                cfg.cluster_name,
                docker or subprocess.run,
                force_unverified_ownership=force_unverified_ownership,
            )
        except KindIdentityError as exc:
            raise CommandError(
                f"refusing to delete kind cluster {cfg.cluster_name!r}: {exc}"
            ) from exc
        kubeconfig = cfg.kubeconfig_path if cfg.kubeconfig_path.is_file() else None
        args = ["kind", "delete", "cluster", "--name", cfg.cluster_name]
        if kubeconfig is not None:
            args += ["--kubeconfig", str(kubeconfig)]
        tool(args)  # raises on failure: the ownership record is kept
        if cfg.cluster_name in tool(["kind", "get", "clusters"]).split():
            raise CommandError(
                f"kind delete reported success, but {cfg.cluster_name!r} is still listed;"
                f" ownership record kept at {record}"
            )
        print(f"deleted kind cluster {cfg.cluster_name}")
    else:
        print(f"kind cluster {cfg.cluster_name} does not exist")
    record.unlink(missing_ok=True)  # only once the cluster is established absent
    for path in (
        cfg.kubeconfig_path,
        cfg.harness_kubeconfig_path,
        cfg.harness_token_meta_path,
        cfg.reader_kubeconfig_path,
        cfg.reader_token_meta_path,
        cfg.kubeconfig_path.parent / "images.json",
    ):
        path.unlink(missing_ok=True)
    for sub in ("images", "charts", "render"):
        shutil.rmtree(cfg.kubeconfig_path.parent / sub, ignore_errors=True)


def main(argv: list[str]) -> int:
    """The bootstrap entry point. The ownership override is argv only (decision 0022):
    `down --force-unverified-ownership`, exactly, for one invocation. Nothing else, and no
    environment variable or configuration, can enable it, so a quarantine rebuild child
    (`python -m safeops.bootstrap down`, which inherits the operator's environment) never
    can."""
    from safeops.kind_identity import FORCE_FLAG

    commands = {"up": up, "down": down, "render-scope": render_scope}
    force = argv == ["down", FORCE_FLAG]
    if not force and (len(argv) != 1 or argv[0] not in commands):
        print(
            f"usage: python -m safeops.bootstrap up|down|render-scope | down {FORCE_FLAG}",
            file=sys.stderr,
        )
        return 2
    try:
        if force:
            print(
                "[cluster-down] WARNING: --force-unverified-ownership: deleting without the"
                " exact container-ID ownership check (the kind labels are still checked)",
                file=sys.stderr,
            )
            down(force_unverified_ownership=True)
        else:
            commands[argv[0]]()
    except (CommandError, CredentialError, GuardError, ValueError) as exc:
        print(f"[cluster-{argv[0]}] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
