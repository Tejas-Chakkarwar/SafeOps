"""Information wall (design §6, Rule 1; decision 0020): the agent, the feature extractor
and the gates never reach evaluator or harness state.

The boundary is by namespace. Everything under a FORBIDDEN root is evaluator or harness
code: fault ground truth (safeops.faults), the experimental unit, runner and run records
(safeops.experiment), verdicts (safeops.verify), the harness credential and measurement
path (safeops.environment), the admin credential (safeops.bootstrap), and analysis over
records (safeops.analysis).

The invariant is the boundary, not an allowlist: protected code may depend on any neutral
runtime module, but never reaches a forbidden root, transitively. The neutral modules named
below are checked as protected so they stay neutral; they are examples, not the complete
set protected code may use (decision 0021). safeops.tools is protected before the first
real tool lands: tools are the agent's senses.

The check is transitive, follows relative imports, and sees imports inside function bodies.
Dynamic imports (importlib) are not caught, so don't use them in those packages.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# The protected packages, plus the neutral modules they consume: those must stay neutral too.
PROTECTED = (
    "safeops.agent",
    "safeops.tools",
    "safeops.features",
    "safeops.gate",
    "safeops.context",
    "safeops.domain",
    "safeops.enums",
    "safeops.topology",
    "safeops.cluster",  # the guarded client the tools read through (Milestone 6)
    "safeops.actions",  # structural action contracts: parser now, executor at M8B (0025 D§3.4)
)
FORBIDDEN = (
    "safeops.faults",
    "safeops.experiment",
    "safeops.verify",
    "safeops.environment",
    "safeops.bootstrap",
    "safeops.analysis",
)


def _module_index(root: Path, top: str) -> dict[str, Path]:
    index = {}
    for path in (root / top).rglob("*.py"):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        index[".".join(parts)] = path
    return index


def _imports(name: str, path: Path, top: str) -> set[str]:
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)
    # Importing a.b.c runs a/__init__ and a/b/__init__ too.
    with_parents = set()
    for mod in found:
        parts = mod.split(".")
        with_parents.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    return {m for m in with_parents if m == top or m.startswith(top + ".")}


def reachable_modules(root: Path, top: str, roots: tuple[str, ...]) -> set[str]:
    index = _module_index(root, top)
    todo = [m for m in index if any(m == r or m.startswith(r + ".") for r in roots)]
    seen: set[str] = set()
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        if mod in index:
            todo.extend(_imports(mod, index[mod], top) - seen)
    return seen


def _leaks(reachable: set[str], forbidden: tuple[str, ...] | str) -> set[str]:
    roots = (forbidden,) if isinstance(forbidden, str) else forbidden
    return {m for m in reachable for f in roots if m == f or m.startswith(f + ".")}


def test_protected_packages_exist() -> None:
    index = _module_index(REPO_ROOT, "safeops")
    for pkg in PROTECTED:
        assert pkg in index, f"{pkg} missing, so the wall test would pass vacuously"


def test_forbidden_roots_exist() -> None:
    """The mirror of the test above: a renamed or deleted forbidden root would make the wall
    pass vacuously."""
    index = _module_index(REPO_ROOT, "safeops")
    for root in FORBIDDEN:
        assert root in index, f"{root} missing, so the wall test would pass vacuously"


def test_ground_truth_modules_are_behind_the_wall() -> None:
    """The modules that hold evaluator ground truth today, named so a move is noticed."""
    for module in (
        "safeops.faults.spec",
        "safeops.faults.injector",
        "safeops.experiment.unit",  # RunUnit: fault_id, fault_seed, trial_index
        "safeops.experiment.schema",
        "safeops.experiment.records",
        "safeops.environment.workload",  # the evaluator's measurement path
    ):
        assert _leaks({module}, FORBIDDEN), f"{module} is outside every forbidden root"


def test_neutral_types_are_reachable_without_a_leak() -> None:
    """Protected code may use the shared domain types; that must not open the wall."""
    reachable = reachable_modules(REPO_ROOT, "safeops", ("safeops.domain",))
    assert "safeops.domain" in reachable and not _leaks(reachable, FORBIDDEN)
    from safeops.domain import FeatureVector
    from safeops.experiment.schema import FeatureVector as RecordFeatureVector

    assert FeatureVector is RecordFeatureVector  # one model, reused, never duplicated


def test_agent_features_gate_never_reach_faults() -> None:
    reachable = reachable_modules(REPO_ROOT, "safeops", PROTECTED)
    assert not _leaks(reachable, FORBIDDEN), (
        f"information wall breached: {sorted(_leaks(reachable, FORBIDDEN))}"
    )


def test_wall_checker_catches_transitive_and_relative_imports(tmp_path: Path) -> None:
    """The checker itself must be able to fail."""
    pkg = tmp_path / "safeops"
    for sub in ("agent", "tools", "faults", "gate"):
        (pkg / sub).mkdir(parents=True)
        (pkg / sub / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "faults" / "spec.py").write_text("GROUND_TRUTH = 1\n")
    # agent -> tools (absolute) -> faults (relative): transitive leak.
    (pkg / "agent" / "loop.py").write_text("from safeops.tools import helper\n")
    (pkg / "tools" / "helper.py").write_text("from ..faults.spec import GROUND_TRUTH\n")
    # gate stays clean.
    (pkg / "gate" / "always_act.py").write_text("import json\n")

    assert _leaks(reachable_modules(tmp_path, "safeops", ("safeops.agent",)), FORBIDDEN)
    assert not _leaks(reachable_modules(tmp_path, "safeops", ("safeops.gate",)), FORBIDDEN)


def _fake_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    pkg = tmp_path / "safeops"
    for sub in (
        "agent",
        "features",
        "gate",
        "tools",
        "faults",
        "experiment",
        "environment",
        "verify",
    ):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
        (pkg / sub / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    for rel, text in files.items():
        (pkg / rel).write_text(text)
    return tmp_path


def test_every_forbidden_root_is_caught_directly(tmp_path: Path) -> None:
    root = _fake_tree(
        tmp_path,
        {
            "experiment/unit.py": "class RunUnit: ...\n",
            "environment/workload.py": "X = 1\n",
            "agent/loop.py": "from safeops.experiment.unit import RunUnit\n",
            "gate/always_act.py": "import safeops.environment.workload\n",
        },
    )
    leaked = _leaks(reachable_modules(root, "safeops", ("safeops.agent",)), FORBIDDEN)
    assert {"safeops.experiment", "safeops.experiment.unit"} <= leaked
    assert _leaks(reachable_modules(root, "safeops", ("safeops.gate",)), FORBIDDEN)


def test_a_lazy_import_inside_a_function_is_caught(tmp_path: Path) -> None:
    root = _fake_tree(
        tmp_path,
        {
            "experiment/unit.py": "class RunUnit: ...\n",
            "features/extractor.py": (
                "def extract():\n"
                "    from safeops.experiment.unit import RunUnit\n"
                "    return RunUnit\n"
            ),
        },
    )
    assert _leaks(reachable_modules(root, "safeops", ("safeops.features",)), FORBIDDEN)


@pytest.mark.parametrize(
    "target",
    [
        "safeops.environment.workload",
        "safeops.experiment.unit",
        "safeops.faults.spec",
        "safeops.verify.harm",
    ],
)
def test_a_tool_cannot_reach_evaluator_state(tmp_path: Path, target: str) -> None:
    """A tool is protected on its own, not only through the agent that imports it."""
    pkg, mod = target.rsplit(".", 1)
    root = _fake_tree(
        tmp_path,
        {
            f"{pkg.split('.', 1)[1]}/{mod}.py": "X = 1\n",
            "tools/get_metrics.py": f"from {pkg} import {mod}\n",
        },
    )
    leaked = _leaks(reachable_modules(root, "safeops", ("safeops.tools",)), FORBIDDEN)
    assert pkg in leaked


def test_tools_are_in_the_protected_set() -> None:
    assert "safeops.tools" in PROTECTED


def test_a_relative_import_of_the_experiment_is_caught(tmp_path: Path) -> None:
    root = _fake_tree(
        tmp_path,
        {
            "experiment/unit.py": "class RunUnit: ...\n",
            "tools/helper.py": "from ..experiment import unit\n",
            "agent/loop.py": "from safeops.tools import helper\n",
        },
    )
    assert _leaks(reachable_modules(root, "safeops", ("safeops.agent",)), FORBIDDEN)


def test_the_real_tools_reach_the_reader_client_and_nothing_forbidden() -> None:
    """Milestone 6 (decision 0023): with the eight real tools in place, the wall still holds,
    and the traversal really walks them (not a vacuous pass)."""
    reachable = reachable_modules(REPO_ROOT, "safeops", ("safeops.tools",))
    assert {
        "safeops.tools.kube",
        "safeops.tools.metrics",
        "safeops.tools.contracts",
        "safeops.tools.canonical",
        "safeops.tools.reader",
        "safeops.cluster",
        "safeops.topology",
    } <= reachable
    assert not _leaks(reachable, FORBIDDEN), sorted(_leaks(reachable, FORBIDDEN))


@pytest.mark.parametrize(
    "target",
    [
        "safeops.environment.clock",
        "safeops.experiment.schema",
        "safeops.faults.injector",
        "safeops.verify.effectiveness",
    ],
)
def test_a_tool_reaching_evaluator_state_through_a_neutral_helper_is_caught(
    tmp_path: Path, target: str
) -> None:
    """A "neutral" helper that itself imports harness code doesn't launder the import."""
    pkg, mod = target.rsplit(".", 1)
    root = _fake_tree(
        tmp_path,
        {
            f"{pkg.split('.', 1)[1]}/{mod}.py": "X = 1\n",
            "cluster.py": f"def helper():\n    from {pkg} import {mod}\n",
            "tools/kube.py": "from safeops.cluster import helper\n",
        },
    )
    assert pkg in _leaks(reachable_modules(root, "safeops", ("safeops.tools",)), FORBIDDEN)
