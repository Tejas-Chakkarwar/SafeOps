#!/usr/bin/env python3
"""Repository policy checks for SafeOps (decision 0019). Standard library only.

    python scripts/check_repo_policy.py --base <rev> --head <rev>

Three policies, each fail-closed: if a check can't establish that its policy holds (a
Git command fails, a revision doesn't resolve, the history it needs is missing), it fails
with a diagnostic. It never passes for lack of evidence.

1. Frozen design: no commit in base..head may modify docs/safeops-design.md unless that
   exact commit's message carries [allow-design-change]. Needs the commit range.
2. Tracked-artifact hygiene: run data, local state, the reference repositories, env files
   and kubeconfigs must never be tracked. Whole tracked tree (git ls-files).
3. Decision numbering: docs/decisions/NNNN-*.md are 0001..N without gaps or duplicates;
   the only non-numbered file allowed is reference-review.md. Whole tracked tree.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath

DESIGN_DOC = "docs/safeops-design.md"
DESIGN_MARKER = "[allow-design-change]"
DECISIONS_DIR = "docs/decisions"
# Exact filenames, never patterns. A new exception is a deliberate policy change.
DECISION_EXCEPTIONS = frozenset({"reference-review.md"})
_NUMBERED = re.compile(r"(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_ZERO_SHA = re.compile(r"0+")

# Anchored at the repository root: `reference/` is the root tree only, so
# docs/decisions/reference-review.md is untouched by it.
_FORBIDDEN_ROOT_DIRS = ("data/runs/", ".safeops/", "reference/")

Git = Callable[[list[str]], str]


class PolicyError(Exception):
    """A policy couldn't be checked. Reported as a failure, never as a pass."""


def run_git(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PolicyError(f"git {' '.join(args)} could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise PolicyError(
            f"git {' '.join(args)} failed: {detail[-1] if detail else f'exit {proc.returncode}'}"
        )
    return proc.stdout


# --- 2. tracked-artifact hygiene ---------------------------------------------------------------


def forbidden_reason(path: str) -> str | None:
    """Why a tracked path is forbidden, or None. `path` is repository-relative."""
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return "not a normalized repository-relative path"
    norm = p.as_posix()
    for root in _FORBIDDEN_ROOT_DIRS:
        if norm.startswith(root) or norm == root.rstrip("/"):
            return f"under {root} (generated, local or third-party state)"
    name = p.name
    if name == ".env" or name.startswith(".env."):
        return "an environment file (.env, .env.*)"
    if name == "kubeconfig" or name.endswith(".kubeconfig"):
        return "a kubeconfig"
    return None


def hygiene_problems(paths: Iterable[str]) -> list[str]:
    problems = []
    for path in paths:
        reason = forbidden_reason(path)
        if reason is not None:
            problems.append(f"tracked file {path} is forbidden: {reason}")
    return problems


# --- 3. decision numbering ---------------------------------------------------------------------


def decision_problems(paths: Iterable[str]) -> list[str]:
    """Problems with the decision log, given every tracked path."""
    problems = []
    numbers: dict[int, list[str]] = {}
    prefix = DECISIONS_DIR + "/"
    for path in paths:
        if not path.startswith(prefix):
            continue
        name = path[len(prefix) :]
        if "/" in name:
            problems.append(f"{path}: decisions are flat files, not subdirectories")
            continue
        m = _NUMBERED.fullmatch(name)
        if m is not None:
            numbers.setdefault(int(m.group(1)), []).append(name)
        elif name not in DECISION_EXCEPTIONS:
            problems.append(
                f"{path}: not a numbered decision (NNNN-lowercase-words.md) and not one of"
                f" the named exceptions {sorted(DECISION_EXCEPTIONS)}"
            )
    for n, names in sorted(numbers.items()):
        if len(names) > 1:
            problems.append(f"decision number {n:04d} is used by {sorted(names)}")
    if numbers:
        missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
        if missing:
            problems.append(
                "decision numbers have gaps: missing "
                + ", ".join(f"{n:04d}" for n in missing)
                + " (supersede an obsolete decision; never delete it)"
            )
    if 0 in numbers:
        problems.append("decision numbers start at 0001")
    return problems


# --- 1. frozen design --------------------------------------------------------------------------


def resolve_commit(rev: str | None, role: str, git: Git) -> str:
    """The full SHA for `rev`, or PolicyError. Missing, all-zero, malformed or unknown
    revisions are refused: an unusable range must never read as "nothing changed"."""
    if rev is None or not rev.strip():
        raise PolicyError(f"no {role} revision was supplied")
    rev = rev.strip()
    if _ZERO_SHA.fullmatch(rev):
        raise PolicyError(f"{role} is the all-zero SHA (a new branch or deleted ref)")
    if rev.startswith("-"):
        raise PolicyError(f"{role} revision {rev!r} is malformed")
    try:
        sha = git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"]).strip()
    except PolicyError as exc:
        raise PolicyError(f"{role} {rev!r} is not a commit in this clone: {exc}") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PolicyError(f"{role} {rev!r} resolved to {sha!r}, not a commit SHA")
    return sha


def design_problems(base: str | None, head: str | None, git: Git = run_git) -> list[str]:
    """Commits in base..head that modify the design without their own marker."""
    base_sha = resolve_commit(base, "base", git)
    head_sha = resolve_commit(head, "head", git)
    touching = git(
        ["rev-list", "--full-history", f"{base_sha}..{head_sha}", "--", DESIGN_DOC]
    ).split()
    problems = []
    for sha in touching:
        message = git(["log", "-1", "--format=%B", sha])
        if DESIGN_MARKER not in message:
            subject = message.strip().splitlines()[0] if message.strip() else ""
            problems.append(
                f"{DESIGN_DOC} is frozen, and commit {sha[:12]} ({subject!r}) modifies it."
                f" An intentional design change needs {DESIGN_MARKER} in that exact"
                " commit's message."
            )
    return problems


# --- the runner ----------------------------------------------------------------------------------


def tracked_paths(git: Git = run_git) -> list[str]:
    out = git(["ls-files", "-z"])
    paths = [p for p in out.split("\0") if p]
    if not paths:
        raise PolicyError("git ls-files returned no tracked files")
    return paths


def run(base: str | None, head: str | None, git: Git = run_git) -> int:
    """Run every policy; print a line per result. Exit status 0 only if all passed."""
    failed = False

    def check(name: str, fn: Callable[[], list[str]], unchecked: str) -> None:
        nonlocal failed
        try:
            problems = fn()
        except PolicyError as exc:
            problems = [f"{unchecked}: {exc}"]
        except Exception as exc:  # an unexpected condition is a failure, never a pass
            problems = [f"{unchecked}: unexpected {type(exc).__name__}: {exc}"]
        if problems:
            failed = True
            print(f"FAIL {name}")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"ok   {name}")

    check(
        "frozen design",
        lambda: design_problems(base, head, git),
        "the design-freeze policy could not establish a valid commit range",
    )
    check(
        "tracked-artifact hygiene",
        lambda: hygiene_problems(tracked_paths(git)),
        "the hygiene policy could not read the tracked tree",
    )
    check(
        "decision numbering",
        lambda: decision_problems(tracked_paths(git)),
        "the decision-numbering policy could not read the tracked tree",
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the range's base revision (excluded)")
    parser.add_argument("--head", required=True, help="the range's head revision (included)")
    args = parser.parse_args(argv)
    return run(args.base, args.head)


if __name__ == "__main__":
    sys.exit(main())
