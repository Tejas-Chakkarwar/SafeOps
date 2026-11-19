"""Rendering SafeOps manifests and Helm values from config (decisions 0006, 0007).

Placeholders are ${UPPER_CASE}. Rendering is strict: an unknown placeholder is an error,
so a typo can never deploy a literal "${...}" or a default. Other "$" text, such as
Prometheus relabel "$1:$2", passes through untouched.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from safeops.cluster import REPO_ROOT, ClusterConfig

MANIFEST_DIR = REPO_ROOT / "manifests"
TESTBED_SRC = REPO_ROOT / "testbed"
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# Files that go into each image (decision 0007). A content tag is derived from exactly
# these, so the tag changes only when that image's inputs change.
IMAGE_INPUTS = {
    "testbed": (
        "Dockerfile",
        ".dockerignore",
        "requirements-base.txt",
        "requirements-testbed.txt",
        "app/__init__.py",
        "app/common.py",
        "app/httpbody.py",
        "app/service.py",
    ),
    "loadgen": (
        "Dockerfile",
        ".dockerignore",
        "requirements-base.txt",
        "app/__init__.py",
        "app/common.py",
        "app/httpbody.py",
        "app/window.py",
        "app/loadgen.py",
    ),
}
IMAGE_REPOS = {"testbed": "safeops-testbed", "loadgen": "safeops-loadgen"}


def render(text: str, values: dict[str, str]) -> str:
    def sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"unknown manifest placeholder ${{{key}}}")
        return str(values[key])

    return _PLACEHOLDER.sub(sub, text)


def content_tag(image: str, src: Path = TESTBED_SRC) -> str:
    """Short, content-derived tag for readability. Provenance uses the real digest."""
    h = hashlib.sha256()
    for rel in IMAGE_INPUTS[image]:
        h.update(rel.encode() + b"\0" + (src / rel).read_bytes() + b"\0")
    return h.hexdigest()[:12]


def image_refs(src: Path = TESTBED_SRC) -> dict[str, str]:
    return {image: f"{IMAGE_REPOS[image]}:{content_tag(image, src)}" for image in IMAGE_INPUTS}


def template_values(cfg: ClusterConfig, images: dict[str, str]) -> dict[str, str]:
    return {
        "TESTBED_NAMESPACE": cfg.namespace,
        "SYSTEM_NAMESPACE": cfg.system_namespace,
        "WORKLOAD_NAMESPACE": cfg.workload_namespace,
        "WORKLOAD_SERVICE": cfg.workload_service,
        "WORKLOAD_SERVICE_PORT": str(cfg.workload_service_port),
        "MONITORING_NAMESPACE": cfg.monitoring_namespace,
        "CHAOS_NAMESPACE": cfg.chaos_namespace,
        "POSTGRES_IMAGE": cfg.postgres_image,
        "TESTBED_IMAGE": images["testbed"],
        "LOADGEN_IMAGE": images["loadgen"],
    }


def load_objects(path: Path, values: dict[str, str]) -> list[dict]:
    import yaml

    return [doc for doc in yaml.safe_load_all(render(path.read_text(), values)) if doc]


def manifest_files(group: str) -> list[Path]:
    return sorted((MANIFEST_DIR / group).glob("*.yaml"))
