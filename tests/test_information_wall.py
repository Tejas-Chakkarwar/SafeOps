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
Dynamic imports (importlib, __import__, exec, eval) would escape it, so a separate static
check refuses them anywhere in the protected set (decision 0025 D§15).

safeops.cluster and safeops.tools are protected, not forbidden, so the wall alone doesn't
stop protected code reaching the Kubernetes client. The Milestone 8 read path (parser,
features, gate and shadow scoring, policy) must not: a third check refuses any import of
safeops.cluster or of any safeops.tools module from it, transitively. Every safeops.tools
module counts, contracts and canonical included: importing one runs safeops/tools/__init__,
which imports the kubectl-backed tools (decision 0025, M8A-2 amendment).
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
    "safeops.policy",  # strict policy loading + action semantics: features now, executor at M8B
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


# --- dynamic imports (decision 0025 D§15) --------------------------------------------------------

DYNAMIC = frozenset({"__import__", "exec", "eval"})


def dynamic_imports(path: Path) -> list[str]:
    """Uses of importlib, __import__, exec or eval in a module's code (not its strings or
    comments): imports of importlib, and any reference to the three builtins by name or as
    an attribute (builtins.exec)."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "importlib"]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "importlib":
            found.append(node.module)
        elif isinstance(node, ast.ImportFrom):
            found += [a.name for a in node.names if a.name in DYNAMIC]
        elif isinstance(node, ast.Name) and node.id in DYNAMIC:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in DYNAMIC:
            found.append(node.attr)
    return [f"{path.name}:{name}" for name in found]


def _protected_modules(root: Path) -> dict[str, Path]:
    index = _module_index(root, "safeops")
    return {
        m: p for m, p in index.items() if any(m == r or m.startswith(r + ".") for r in PROTECTED)
    }


def test_the_protected_set_uses_no_dynamic_import() -> None:
    modules = _protected_modules(REPO_ROOT)
    assert {"safeops.policy", "safeops.features.extractor", "safeops.gate.shadow"} <= set(modules)
    found = [hit for path in modules.values() for hit in dynamic_imports(path)]
    assert found == [], f"dynamic imports in protected code: {found}"


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\n",
        "import importlib.util\n",
        "from importlib import import_module\n",
        "def f():\n    from importlib import import_module\n",
        "m = __import__('safeops.faults.spec')\n",
        "eval('1')\n",
        "exec('x = 1')\n",
        "import builtins\nbuiltins.exec('x = 1')\n",
        "run = eval\n",
        "from builtins import __import__\n",
    ],
)
def test_the_dynamic_import_check_catches_each_form(tmp_path: Path, source: str) -> None:
    path = tmp_path / "probe.py"
    path.write_text(source)
    assert dynamic_imports(path)


def test_the_dynamic_import_check_ignores_strings_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "probe.py"
    path.write_text("# eval(x), importlib\nTEXT = 'exec(1) __import__ importlib'\nevaluate = 1\n")
    assert dynamic_imports(path) == []


# --- the M8 read path never reaches the Kubernetes client (decision 0025, M8A-2) -----------------

READ_PATH = ("safeops.agent.parser", "safeops.features", "safeops.gate", "safeops.policy")
NO_CLUSTER = ("safeops.cluster", "safeops.tools")


def read_path_reach(root: Path) -> set[str]:
    """Every module the read-path roots import, transitively, starting from the roots and
    their parent packages (importing safeops.agent.parser runs safeops/agent/__init__)."""
    index = _module_index(root, "safeops")
    todo = [m for m in index if any(m == r or m.startswith(r + ".") for r in READ_PATH)]
    for r in READ_PATH:
        parts = r.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]
    seen: set[str] = set()
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        if mod in index:
            todo.extend(_imports(mod, index[mod], "safeops") - seen)
    return seen


def test_the_read_path_reaches_neither_the_cluster_nor_any_tool() -> None:
    reach = read_path_reach(REPO_ROOT)
    assert {
        "safeops.agent.parser",
        "safeops.features.extractor",
        "safeops.features.evidence",
        "safeops.features.blast",
        "safeops.features.conflict",
        "safeops.features.policy",
        "safeops.gate.always_act",
        "safeops.gate.protocol",
        "safeops.gate.shadow",
        "safeops.policy",
        "safeops.actions",
        "safeops.topology",
    } <= reach  # the traversal really walks the read path
    assert not _leaks(reach, NO_CLUSTER), sorted(_leaks(reach, NO_CLUSTER))


def _read_path_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A miniature package whose safeops.tools initializes the kubectl-backed tools, as the
    real one does: tools/__init__ -> tools.kube -> tools.reader -> safeops.cluster."""
    base = {
        "cluster.py": "class Kubectl: ...\n",
        "tools/__init__.py": "from safeops.tools.kube import get_deployment\n",
        "tools/kube.py": "from safeops.tools.reader import read\ndef get_deployment(): ...\n",
        "tools/reader.py": "from safeops.cluster import Kubectl\ndef read(): ...\n",
        "tools/contracts.py": "class DeploymentData: ...\n",
        "tools/canonical.py": "def result_digest(x): ...\n",
        "policy.py": "import hashlib\n",
        "agent/parser.py": "import json\n",
        "gate/shadow.py": "import hashlib\n",
        "features/extractor.py": "from safeops.features import helper\n",
        "features/helper.py": "X = 1\n",
    }
    base.update(files)
    return _fake_tree(tmp_path, base)


def test_the_clean_miniature_read_path_passes(tmp_path: Path) -> None:
    root = _read_path_tree(tmp_path, {})
    assert not _leaks(read_path_reach(root), NO_CLUSTER)


@pytest.mark.parametrize(
    ("where", "source", "leak"),
    [
        ("features/helper.py", "from safeops.cluster import Kubectl\n", "safeops.cluster"),
        (
            "features/helper.py",
            "from safeops.tools.contracts import DeploymentData\n",
            "safeops.tools",
        ),
        ("features/helper.py", "import safeops.tools.canonical\n", "safeops.tools"),
        ("features/helper.py", "def f():\n    from ..tools import canonical\n", "safeops.tools"),
        ("gate/shadow.py", "from safeops.tools.canonical import result_digest\n", "safeops.tools"),
        ("policy.py", "import safeops.cluster\n", "safeops.cluster"),
        ("agent/parser.py", "from safeops.tools import contracts\n", "safeops.tools"),
    ],
    ids=[
        "cluster",
        "tools-contracts",
        "tools-canonical",
        "relative-lazy",
        "gate",
        "policy",
        "parser",
    ],
)
def test_the_read_path_check_catches_each_reach(tmp_path: Path, where, source, leak) -> None:
    root = _read_path_tree(tmp_path, {where: source})
    leaked = _leaks(read_path_reach(root), NO_CLUSTER)
    assert leak in leaked
    if leak == "safeops.tools":  # and, through the package's own __init__, the client
        assert "safeops.cluster" in leaked
