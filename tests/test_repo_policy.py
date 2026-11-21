"""The repository-policy checker (scripts/check_repo_policy.py; decision 0019).

Path policies are exercised as pure functions, never by changing this checkout. The
design-freeze cases run against throwaway Git repositories with an isolated configuration
(no global or system Git config), plus injected Git failures.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_repo_policy", REPO_ROOT / "scripts/check_repo_policy.py"
)
policy = importlib.util.module_from_spec(_spec)
sys.modules["check_repo_policy"] = policy
_spec.loader.exec_module(policy)

# --- tracked-artifact hygiene ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "data/runs/foo.json",
        "data/runs/rejected/x.json",
        ".safeops/kubeconfig",
        ".safeops/images.json",
        "reference/foo.md",
        "reference/AIOpsLab/README.md",
        ".env",
        ".env.local",
        "config/.env.production",
        "foo.kubeconfig",
        "kind/kubeconfig",
        "deep/dir/admin.kubeconfig",
    ],
)
def test_forbidden_tracked_paths_fail(path: str) -> None:
    assert policy.forbidden_reason(path) is not None
    assert policy.hygiene_problems([path])


@pytest.mark.parametrize(
    "path",
    [
        "docs/decisions/reference-review.md",  # the one near-overlap: root reference/ only
        "safeops/cluster.py",
        "docs/kubeconfig-handling.md",  # mentions kubeconfig, isn't one
        "tests/test_kubeconfig.py",
        "data/README.md",  # data/ itself isn't forbidden, data/runs/ is
        "docs/reference/notes.md",
        "environment.md",
        ".envrc-notes.md",
    ],
)
def test_ordinary_tracked_paths_pass(path: str) -> None:
    assert policy.forbidden_reason(path) is None


def test_no_env_template_exception_was_invented() -> None:
    assert policy.forbidden_reason(".env.example") is not None


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py"])
def test_non_relative_paths_fail_closed(path: str) -> None:
    assert policy.forbidden_reason(path) is not None


# --- decision numbering ----------------------------------------------------------------------


def _decisions(*names: str) -> list[str]:
    return [f"docs/decisions/{n}" for n in names] + ["safeops/cli.py"]


GOOD = ("0001-a.md", "0002-b-c.md", "0003-d.md", "reference-review.md")


def test_contiguous_decisions_with_the_named_exception_pass() -> None:
    assert policy.decision_problems(_decisions(*GOOD)) == []


@pytest.mark.parametrize(
    ("names", "match"),
    [
        (GOOD + ("0003-again.md",), "0003 is used by"),
        (("0001-a.md", "0003-c.md"), "missing 0002"),
        (GOOD + ("0005-e.md",), "missing 0004"),
        (GOOD + ("19-x.md",), "not a numbered decision"),
        (GOOD + ("0004_x.md",), "not a numbered decision"),
        (GOOD + ("0004-.md",), "not a numbered decision"),
        (GOOD + ("0004-Upper.md",), "not a numbered decision"),
        (GOOD + ("0004-x.txt",), "not a numbered decision"),
        (GOOD + ("random-notes.md",), "not a numbered decision"),
        (("0001-a.md", "reference-review.md", "notes.md"), "not a numbered decision"),
        (("0000-zero.md", "0001-a.md"), "start at 0001"),
        (GOOD + ("sub/0004-x.md",), "flat files"),
    ],
)
def test_bad_decision_logs_fail(names, match) -> None:
    problems = policy.decision_problems(_decisions(*names))
    assert any(match in p for p in problems), problems


def test_the_exception_is_an_exact_name_not_a_pattern() -> None:
    assert policy.DECISION_EXCEPTIONS == frozenset({"reference-review.md"})
    assert policy.decision_problems(_decisions("0001-a.md", "reference-review-2.md"))


# --- the real repository ----------------------------------------------------------------------


def _real_git(args: list[str]) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=True
    ).stdout


def test_the_real_tracked_tree_passes_hygiene_and_numbering() -> None:
    paths = policy.tracked_paths(_real_git)
    assert "docs/decisions/reference-review.md" in paths
    assert policy.hygiene_problems(paths) == []
    assert policy.decision_problems(paths) == []


# --- frozen design (throwaway repositories) -----------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A Git repository with no global or system configuration."""
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / "repo"
    root.mkdir()

    def git(args: list[str]) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
            ).stdout
        except subprocess.CalledProcessError as exc:
            raise policy.PolicyError(exc.stderr.strip() or f"exit {exc.returncode}") from exc

    def commit(message: str, **files: str) -> str:
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            git(["add", rel])
        git(["commit", "-q", "--allow-empty", "-m", message])
        return git(["rev-parse", "HEAD"]).strip()

    git(["init", "-q", "-b", "main"])
    base = commit("initial", **{"docs/safeops-design.md": "v1\n", "README.md": "x\n"})
    return git, commit, base


DESIGN = "docs/safeops-design.md"


def test_design_unchanged_passes(repo) -> None:
    git, commit, base = repo
    head = commit("ordinary change", **{"README.md": "y\n"})
    assert policy.design_problems(base, head, git) == []


def test_design_changed_without_the_marker_fails(repo) -> None:
    git, commit, base = repo
    head = commit("tweak the design", **{DESIGN: "v2\n"})
    (problem,) = policy.design_problems(base, head, git)
    assert "is frozen" in problem and head[:12] in problem and "[allow-design-change]" in problem


def test_the_marker_on_the_modifying_commit_passes(repo) -> None:
    git, commit, base = repo
    head = commit("Rev 6 [allow-design-change]", **{DESIGN: "v2\n"})
    assert policy.design_problems(base, head, git) == []


def test_a_marker_elsewhere_in_the_range_authorizes_nothing(repo) -> None:
    git, commit, base = repo
    sneaky = commit("tweak the design", **{DESIGN: "v2\n"})
    head = commit("unrelated [allow-design-change]", **{"README.md": "z\n"})
    problems = policy.design_problems(base, head, git)
    assert len(problems) == 1 and sneaky[:12] in problems[0]


def test_each_modifying_commit_needs_its_own_marker(repo) -> None:
    git, commit, base = repo
    commit("first [allow-design-change]", **{DESIGN: "v2\n"})
    second = commit("second", **{DESIGN: "v3\n"})
    problems = policy.design_problems(base, second, git)
    assert len(problems) == 1 and second[:12] in problems[0]


def test_a_pull_request_style_range_works(repo) -> None:
    """PR base = the target branch's tip; head = the branch tip, not descended from it."""
    git, commit, base = repo
    git(["checkout", "-q", "-b", "feature"])
    feature = commit("feature work", **{"README.md": "f\n"})
    git(["checkout", "-q", "main"])
    target = commit("main moved on", **{"other.txt": "m\n"})
    assert policy.design_problems(target, feature, git) == []
    git(["checkout", "-q", "feature"])
    bad = commit("feature edits the design", **{DESIGN: "v9\n"})
    assert policy.design_problems(target, bad, git)


def test_an_empty_range_passes(repo) -> None:
    git, _, base = repo
    assert policy.design_problems(base, base, git) == []


@pytest.mark.parametrize(
    ("base", "head", "match"),
    [
        ("0" * 40, "HEAD", "all-zero"),
        (None, "HEAD", "no base"),
        ("", "HEAD", "no base"),
        ("HEAD", None, "no head"),
        ("not-a-sha", "HEAD", "not a commit"),
        ("deadbeef" * 5, "HEAD", "not a commit"),
        ("--all", "HEAD", "malformed"),
    ],
)
def test_an_unusable_range_fails_closed(repo, base, head, match) -> None:
    git, _, _ = repo
    with pytest.raises(policy.PolicyError, match=match):
        policy.design_problems(base, head, git)


def test_a_base_missing_from_a_shallow_or_rewritten_history_fails_closed(repo) -> None:
    git, commit, base = repo
    head = commit("x", **{"README.md": "q\n"})
    git(["reset", "-q", "--hard", base])
    git(["reflog", "expire", "--expire=now", "--all"])
    git(["gc", "-q", "--prune=now"])
    with pytest.raises(policy.PolicyError, match="not a commit"):
        policy.design_problems(head, base, git)  # the force-pushed-away commit is gone


def test_a_git_failure_fails_closed() -> None:
    def broken(args: list[str]) -> str:
        raise policy.PolicyError("fatal: not a git repository")

    with pytest.raises(policy.PolicyError):
        policy.design_problems("HEAD~1", "HEAD", broken)


def test_run_reports_failure_when_a_policy_cannot_be_checked(capsys) -> None:
    def broken(args: list[str]) -> str:
        raise policy.PolicyError("fatal: not a git repository")

    assert policy.run("a" * 40, "b" * 40, broken) == 1
    out = capsys.readouterr().out
    assert "could not establish a valid commit range" in out
    assert "FAIL tracked-artifact hygiene" in out and "FAIL decision numbering" in out


def test_run_turns_an_unexpected_error_into_a_failure(capsys) -> None:
    def weird(args: list[str]) -> str:
        raise RuntimeError("surprise")

    assert policy.run("a" * 40, "b" * 40, weird) == 1
    assert "unexpected RuntimeError" in capsys.readouterr().out


def test_an_empty_tracked_tree_is_not_a_pass() -> None:
    with pytest.raises(policy.PolicyError, match="no tracked files"):
        policy.tracked_paths(lambda args: "")


def test_the_cli_requires_both_revisions() -> None:
    with pytest.raises(SystemExit) as exc:
        policy.main(["--head", "HEAD"])
    assert exc.value.code == 2
