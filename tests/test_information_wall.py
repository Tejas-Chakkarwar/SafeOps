"""Information wall (design §6, Rule 1): agent, features and gate never import safeops.faults.

The check is transitive. If the agent imports a module that imports safeops.faults, that
leaks ground truth just as a direct import would. Dynamic imports (importlib) are not
caught, so don't use them in those packages.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTECTED = ("safeops.agent", "safeops.features", "safeops.gate")
FORBIDDEN = "safeops.faults"


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


def _leaks(reachable: set[str], forbidden: str) -> set[str]:
    return {m for m in reachable if m == forbidden or m.startswith(forbidden + ".")}


def test_protected_packages_exist() -> None:
    index = _module_index(REPO_ROOT, "safeops")
    for pkg in PROTECTED:
        assert pkg in index, f"{pkg} missing, so the wall test would pass vacuously"


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
