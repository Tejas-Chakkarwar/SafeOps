"""Cross-milestone seam review (decision 0020): invariants shared across milestone
boundaries. The information-wall half is in tests/test_information_wall.py."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import random
from pathlib import Path

import pytest

from safeops.environment import snapshot as snap
from safeops.environment.desired import desired_snapshot
from safeops.environment.snapshot import Snapshot

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- 2. an empty diff and an equal digest mean the same identity -------------------------------


def test_a_key_with_none_is_not_a_missing_key() -> None:
    """The regression: diff() said these were identical while their digests differed."""
    present = Snapshot({("ConfigMap", "c"): {"x": None}})
    absent = Snapshot({("ConfigMap", "c"): {}})
    assert present.digest() != absent.digest()
    assert snap.diff(present, absent) == ["ConfigMap/c.x: missing (expected None)"]
    assert snap.diff(absent, present) == ["ConfigMap/c.x: unexpected (got None)"]


@pytest.mark.parametrize(("a", "b"), [(1, 1.0), (True, 1), (False, 0), (0, 0.0)])
def test_leaves_that_python_calls_equal_but_json_does_not_differ(a, b) -> None:
    """1 == 1.0 == True in Python, but their JSON (and so the digest) differs."""
    x, y = Snapshot({("Service", "s"): {"v": a}}), Snapshot({("Service", "s"): {"v": b}})
    assert x.digest() != y.digest()
    assert snap.diff(x, y) != []


def _paths(value, prefix=()):
    """Every (container, key) position in a nested projected value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield (prefix, k)
            yield from _paths(v, prefix + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield (prefix, i)
            yield from _paths(v, prefix + (i,))


def _mutations(base: Snapshot, rng: random.Random, n: int) -> list[Snapshot]:
    """Real projected snapshots, perturbed the ways a projection can change: a value, a
    type (1 vs 1.0 vs True), a key removed, a key set to None, a list element, a resource
    removed or added."""
    out = [base, Snapshot(copy.deepcopy(base.resources))]  # an identical copy
    keys = sorted(base.resources)
    for _ in range(n):
        res = copy.deepcopy(base.resources)
        key = rng.choice(keys)
        positions = list(_paths(res[key]))
        kind = rng.choice(["value", "type", "drop", "none", "list", "remove", "add"])
        if kind == "remove":
            del res[key]
        elif kind == "add":
            res[("ConfigMap", f"rogue-{rng.randrange(1000)}")] = {"data_sha256": "x"}
        elif positions:
            prefix, last = rng.choice(positions)
            parent = res[key]
            for step in prefix:
                parent = parent[step]
            if kind == "drop" and isinstance(parent, dict):
                del parent[last]
            elif kind == "none":
                parent[last] = None
            elif kind == "type" and isinstance(parent[last], int | float):
                parent[last] = float(parent[last]) if isinstance(parent[last], int) else 1
            elif kind == "list" and isinstance(parent, list):
                parent.append(copy.deepcopy(parent[last]))
            else:
                parent[last] = f"changed-{rng.randrange(1000)}"
        out.append(Snapshot(res))
    return out


def test_empty_diff_iff_equal_digest_over_real_projections() -> None:
    """Over the real desired-state projection (Deployments, Services, ConfigMaps, the
    StatefulSet, the PVC; nested dicts, lists and null fields), perturbed 60 ways."""
    base = desired_snapshot()
    kinds = {k for k, _ in base.resources}
    assert {"Deployment", "Service", "ConfigMap", "StatefulSet", "PersistentVolumeClaim"} <= kinds
    snapshots = _mutations(base, random.Random(20), 60)
    for i, a in enumerate(snapshots):
        for b in snapshots[i:]:
            same_digest = a.digest() == b.digest()
            assert (snap.diff(a, b) == []) == same_digest
            assert (snap.diff(b, a) == []) == same_digest


def test_diff_refuses_to_compare_projection_versions() -> None:
    old = Snapshot({}, projection_version=1)
    with pytest.raises(ValueError, match="can't compare projection versions"):
        snap.diff(old, Snapshot({}))


# --- 3. one authoritative PROJECTED_KINDS ----------------------------------------------------


def test_projected_kinds_has_one_definition() -> None:
    from safeops import topology

    assert snap.PROJECTED_KINDS is topology.PROJECTED_KINDS
    assignments = []
    for path in (REPO_ROOT / "safeops").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else ([node.target] if isinstance(node, ast.AnnAssign) else [])
            )
            if any(isinstance(t, ast.Name) and t.id == "PROJECTED_KINDS" for t in targets):
                assignments.append(path.relative_to(REPO_ROOT).as_posix())
    assert assignments == ["safeops/topology.py"], assignments


# --- 4. persisted snapshots carry their projection version ----------------------------------


GOLDEN = Snapshot(
    {
        ("Deployment", "orders"): {"replicas": 2, "image": "img:1", "probe": None},
        ("Service", "orders-svc"): {"ready_endpoint_count": 2, "ports": [{"port": 8080}]},
    }
)


def test_the_canonical_digest_is_unchanged() -> None:
    """digest() still hashes the unversioned canonical form, so every recorded snapshot
    hash and desired_state_hash keeps its meaning."""
    assert GOLDEN.digest() == (
        "sha256:eff708f69a5939fb3fd6107539584fed85869fc2515486e51c0259acd4c526d6"
    )
    assert GOLDEN.digest() == "sha256:" + hashlib.sha256(GOLDEN.to_json().encode()).hexdigest()
    assert "projection_version" not in GOLDEN.to_json()


def test_a_persisted_snapshot_round_trips_with_its_version() -> None:
    s = desired_snapshot()
    text = s.to_persisted_json()
    assert json.loads(text)["projection_version"] == snap.SNAPSHOT_PROJECTION_VERSION
    again = Snapshot.from_persisted_json(text)
    assert again.resources == s.resources and again.digest() == s.digest()
    assert again.projection_version == snap.SNAPSHOT_PROJECTION_VERSION


@pytest.mark.parametrize("version", [1, 3, "2", True, None])
def test_another_projection_version_is_refused(version) -> None:
    raw = json.loads(GOLDEN.to_persisted_json())
    raw["projection_version"] = version
    with pytest.raises(ValueError):
        Snapshot.from_persisted_json(json.dumps(raw))


def test_an_unversioned_legacy_snapshot_is_refused() -> None:
    """The canonical form has no version; reading it as a snapshot would stamp the current
    version onto data from an unknown projection."""
    with pytest.raises(ValueError, match="unversioned snapshot is refused"):
        Snapshot.from_persisted_json(GOLDEN.to_json())
    assert not hasattr(Snapshot, "from_json")  # the old silent loader is gone
