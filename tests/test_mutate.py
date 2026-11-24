"""The mutation harness (scripts/mutate.py) and its table (scripts/mutants.toml).

Everything runs in tmp_path with injected Git and an injected test runner: these tests never
mutate this checkout. The one exception reads only: the committed table must load, satisfy
recorded = ported + unreconstructible, and every target must still match exactly once, so a
stale mutant fails `make test` as well as `make mutate`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("safeops_mutate", REPO_ROOT / "scripts/mutate.py")
mutate = importlib.util.module_from_spec(_spec)
sys.modules["safeops_mutate"] = mutate
_spec.loader.exec_module(mutate)

ORIGINAL = b"def f(x):\r\n    if x > 0:\r\n        return 1\n    return 0\n"


def _mutant(**kw) -> mutate.Mutant:
    base = dict(
        id="T1",
        source="test",
        target="pkg/mod.py",
        guard="the positive guard",
        kills=("tests/test_mod.py::test_guard",),
        old="    if x > 0:",
        new="    if False:",
    )
    base.update(kw)
    return mutate.Mutant(**base)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mod.py").write_bytes(ORIGINAL)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_mod.py").write_text("")
    return tmp_path


def _junit(*cases: tuple[str, str, str]) -> str:
    """(name, outcome, message) cases in module tests.test_mod."""
    body = []
    for name, outcome, message in cases:
        inner = {
            "passed": "",
            "failed": f'<failure message="{message}">trace</failure>',
            "error": f'<error message="{message}">trace</error>',
            "skipped": f'<skipped message="{message}"/>',
        }[outcome]
        body.append(f'<testcase classname="tests.test_mod" name="{name}">{inner}</testcase>')
    return f"<testsuites><testsuite>{''.join(body)}</testsuite></testsuites>"


def _runner(returncode=1, cases=(("test_guard", "failed", "assert 1 == 0"),), seen=None):
    def runner(root, nodes):
        if seen is not None:
            seen.append((root / "pkg/mod.py").read_bytes())
        return mutate.TestRun(returncode, _junit(*cases), "")

    return runner


def _clean_git(states=("", "")):
    states = list(states)

    def git(args):
        assert args[0] == "status"
        return states.pop(0) if len(states) > 1 else states[0]

    return git


def _table(*mutants) -> mutate.Table:
    return mutate.Table({}, tuple(mutants), ())


# --- exact target matching --------------------------------------------------------------------


def test_a_target_that_occurs_once_is_mutated() -> None:
    text = ORIGINAL.decode()
    assert mutate.apply_mutation(text, _mutant()) == text.replace("if x > 0:", "if False:")


def test_a_missing_target_is_a_hard_error_naming_the_mutant() -> None:
    with pytest.raises(mutate.HarnessError, match="mutant T1: .* is missing .*stale"):
        mutate.apply_mutation("def f(): pass\n", _mutant())


def test_a_target_that_occurs_twice_is_a_hard_error() -> None:
    with pytest.raises(mutate.HarnessError, match="mutant T1: .* occurs 2 times"):
        mutate.apply_mutation("    if x > 0:\n    if x > 0:\n", _mutant())


def test_a_stale_mutant_fails_the_run_before_anything_is_mutated(tmp_path) -> None:
    root = _repo(tmp_path)
    lines: list[str] = []
    stale = _mutant(id="T2", old="    if x > 99:")
    code = mutate.run(
        root, _table(_mutant(), stale), git=_clean_git(), runner=_runner(), out=lines.append
    )
    assert code == 1 and any("T2" in line and "stale" in line for line in lines)
    assert (root / "pkg/mod.py").read_bytes() == ORIGINAL


# --- proof of application and restoration -----------------------------------------------------


def test_the_mutation_is_proven_in_place_before_tests_run(tmp_path) -> None:
    root = _repo(tmp_path)
    seen: list[bytes] = []
    mutate.run_mutant(root, _mutant(), _runner(seen=seen))
    assert seen == [ORIGINAL.replace(b"if x > 0:", b"if False:")]


def test_an_unapplied_mutation_is_refused() -> None:
    text = ORIGINAL.decode()
    with pytest.raises(mutate.HarnessError, match="not in pkg/mod.py as written"):
        mutate.prove_applied(text, text, _mutant())


def test_a_mutation_that_is_not_python_is_an_error_and_is_restored(tmp_path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(mutate.HarnessError, match="not valid Python"):
        mutate.run_mutant(root, _mutant(new="    if (:"), _runner())
    assert (root / "pkg/mod.py").read_bytes() == ORIGINAL


def test_restoration_writes_back_the_exact_bytes_and_hash(tmp_path) -> None:
    """CRLF and LF mixed: restoration is byte-exact, never a text round trip."""
    root = _repo(tmp_path)
    mutate.run_mutant(root, _mutant(), _runner())
    assert (root / "pkg/mod.py").read_bytes() == ORIGINAL
    assert mutate.sha256((root / "pkg/mod.py").read_bytes()) == mutate.sha256(ORIGINAL)


@pytest.mark.parametrize(
    "exc", [RuntimeError("boom"), KeyboardInterrupt(), mutate.HarnessError("x")]
)
def test_the_target_is_restored_whatever_the_runner_raises(tmp_path, exc) -> None:
    root = _repo(tmp_path)

    def runner(root, nodes):
        raise exc

    with pytest.raises(type(exc)):
        mutate.run_mutant(root, _mutant(), runner)
    assert (root / "pkg/mod.py").read_bytes() == ORIGINAL


def test_a_restoration_that_cannot_be_proven_is_a_restore_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "f.py"
    path.write_bytes(b"a\n")
    monkeypatch.setattr(mutate, "sha256", lambda data: "different")
    with pytest.raises(mutate.RestoreError):
        mutate.restore(path, b"a\n", "expected")


# --- what counts as a kill ----------------------------------------------------------------------


@pytest.fixture
def verdict(tmp_path):
    """classify() over a JUnit report, with node ids resolved against a real test module."""
    root = _repo(tmp_path)

    def judge(returncode, *cases, kills=("tests/test_mod.py::test_guard",)):
        run = mutate.TestRun(returncode, _junit(*cases), "")
        return mutate.classify(_mutant(kills=kills), run, mutate.parse_junit(run.junit, root))

    return judge


def test_a_semantic_failure_of_the_named_test_is_a_kill(verdict) -> None:
    v = verdict(1, ("test_guard", "failed", "assert 1 == 0"))
    assert v.result == "killed" and v.killer == "tests/test_mod.py::test_guard"


def test_a_failing_parametrization_of_a_named_test_is_a_kill(verdict) -> None:
    v = verdict(
        1, ("test_guard[a]", "passed", ""), ("test_guard[b]", "failed", "Failed: DID NOT RAISE")
    )
    assert v.result == "killed"


def test_a_named_test_that_did_not_run_is_not_a_kill(verdict) -> None:
    v = verdict(4, kills=("tests/test_mod.py::test_missing",))
    assert v.result == "error" and "exit 4" in v.detail
    v = verdict(1, ("test_other", "failed", "assert 0"), kills=("tests/test_mod.py::test_missing",))
    assert v.result == "error" and "not executed" in v.detail


@pytest.mark.parametrize("code", [2, 3, 4, 5, None])
def test_collection_usage_internal_errors_and_timeouts_are_not_kills(verdict, code) -> None:
    assert verdict(code, ("test_guard", "failed", "assert 0")).result == "error"


def test_an_error_outside_the_test_body_is_not_a_kill(verdict) -> None:
    v = verdict(1, ("test_guard", "error", "collection failure"))
    assert v.result == "error"


@pytest.mark.parametrize(
    "message",
    ["ImportError: cannot import name x", "SyntaxError: bad", "NameError: name 'y' is not defined"],
)
def test_a_mutant_that_breaks_the_program_is_not_a_kill(verdict, message) -> None:
    assert verdict(1, ("test_guard", "failed", message)).result == "error"


def test_a_skipped_named_test_is_not_a_kill(verdict) -> None:
    assert verdict(0, ("test_guard", "skipped", "needs a cluster")).result == "error"


def test_a_named_test_that_passes_is_a_survivor(verdict) -> None:
    assert verdict(0, ("test_guard", "passed", "")).result == "survived"


# A pytest collection error: JUnit names no module (classname="").
COLLECTION_ERROR = (
    "<testsuites><testsuite>"
    '<testcase classname="" name="test_broken">'
    '<error message="collection failure">ImportError while importing test module</error>'
    "</testcase>"
    '<testcase classname="tests.test_mod" name="test_guard">'
    '<failure message="assert 1 == 0">trace</failure>'
    "</testcase>"
    "</testsuite></testsuites>"
)


def test_an_empty_junit_classname_keeps_the_fallback_node_id(tmp_path) -> None:
    root = _repo(tmp_path)
    assert mutate.junit_nodeid(root, "", "test_broken") == "::test_broken"
    # Dotted reconstruction is unchanged.
    assert mutate.junit_nodeid(root, "tests.test_mod", "test_guard") == (
        "tests/test_mod.py::test_guard"
    )
    assert mutate.junit_nodeid(root, "tests.test_mod.TestX", "test_y") == (
        "tests/test_mod.py::TestX::test_y"
    )
    assert mutate.junit_nodeid(root, "nowhere.mod", "t") == "nowhere.mod::t"


def test_a_collection_error_parses_into_an_error_case(tmp_path) -> None:
    (broken, guard) = mutate.parse_junit(COLLECTION_ERROR, _repo(tmp_path))
    assert (broken.nodeid, broken.outcome) == ("::test_broken", "error")
    assert (guard.nodeid, guard.outcome) == ("tests/test_mod.py::test_guard", "failed")


@pytest.mark.parametrize("returncode", [1, 2])
def test_a_collection_error_is_a_harness_error_never_a_kill(tmp_path, returncode) -> None:
    """Even beside a failing named test: a module that didn't collect is not evidence."""
    root = _repo(tmp_path)
    run = mutate.TestRun(returncode, COLLECTION_ERROR, "")
    v = mutate.classify(_mutant(), run, mutate.parse_junit(run.junit, root))
    assert v.result == "error"
    assert v.killer is None


# --- the run as a whole ---------------------------------------------------------------------------


def test_a_surviving_mutant_fails_the_run(tmp_path) -> None:
    root = _repo(tmp_path)
    code = mutate.run(
        root,
        _table(_mutant()),
        git=_clean_git(),
        runner=_runner(0, (("test_guard", "passed", ""),)),
        out=lambda s: None,
    )
    assert code == 1 and (root / "pkg/mod.py").read_bytes() == ORIGINAL


def test_a_killed_mutant_passes_the_run(tmp_path) -> None:
    root = _repo(tmp_path)
    assert (
        mutate.run(root, _table(_mutant()), git=_clean_git(), runner=_runner(), out=lambda s: None)
        == 0
    )


def test_a_dirty_starting_tree_is_refused_before_anything_is_touched(tmp_path) -> None:
    root = _repo(tmp_path)
    called = []
    lines: list[str] = []
    code = mutate.run(
        root,
        _table(_mutant()),
        git=_clean_git((" M pkg/other.py\0",)),
        runner=lambda r, n: called.append(1),
        out=lines.append,
    )
    assert code == 1 and not called and "not clean" in lines[0]
    assert (root / "pkg/mod.py").read_bytes() == ORIGINAL


def test_a_leftover_change_to_a_target_is_detected(tmp_path) -> None:
    """A runner that leaves another target changed: the harness stops and fails."""
    root = _repo(tmp_path)
    (root / "pkg/two.py").write_text("y = 1\n")
    other = _mutant(id="T2", target="pkg/two.py", old="y = 1", new="y = 2")

    def runner(r, nodes):
        (root / "pkg/two.py").write_text("y = 3\n")
        return mutate.TestRun(1, _junit(("test_guard", "failed", "assert 0")), "")

    lines: list[str] = []
    code = mutate.run(
        root, _table(_mutant(), other), git=_clean_git(), runner=runner, out=lines.append
    )
    assert code == 1 and any("differ" in line for line in lines)


def test_a_tree_that_differs_at_the_end_fails_the_run(tmp_path) -> None:
    root = _repo(tmp_path)
    lines: list[str] = []
    code = mutate.run(
        root,
        _table(_mutant()),
        git=_clean_git(("", "?? stray.tmp\0")),
        runner=_runner(),
        out=lines.append,
    )
    assert code == 1 and any("working tree differs" in line for line in lines)


# --- the table ----------------------------------------------------------------------------------

_ONE = """
[[recorded]]
source = "0099 example"
count = {count}

[[mutant]]
id = "X1"
source = "0099 example"
target = "pkg/mod.py"
guard = "g"
kills = ["tests/test_mod.py::test_guard"]
old = '''
    if x > 0:'''
new = '''
    if False:'''
"""


def test_the_table_keeps_exact_text_and_checks_the_recovery_invariant() -> None:
    table = mutate.load_table(_ONE.format(count=1))
    assert table.mutants[0].old == "    if x > 0:" and mutate.recovery_counts(table) == (1, 1, 0)
    with pytest.raises(mutate.HarnessError, match="recorded 2, but 1 ported"):
        mutate.load_table(_ONE.format(count=2))


def test_duplicate_ids_and_missing_kills_are_refused() -> None:
    doubled = _ONE.format(count=2) + "\n[[mutant]]" + _ONE.split("[[mutant]]")[1]
    with pytest.raises(mutate.HarnessError, match="duplicate"):
        mutate.load_table(doubled)
    with pytest.raises(mutate.HarnessError, match="kills"):
        mutate.load_table(_ONE.format(count=1).replace('["tests/test_mod.py::test_guard"]', "[]"))


def test_the_committed_table_is_valid_and_no_mutant_is_stale() -> None:
    table = mutate.load_table(mutate.TABLE_PATH.read_text())
    recorded, ported, lost = mutate.recovery_counts(table)
    assert recorded == ported + lost == sum(table.recorded.values())
    assert mutate.stale_targets(REPO_ROOT, table.mutants) == []
    for m in table.mutants:
        for node in m.kills:
            assert (REPO_ROOT / node.split("::")[0]).is_file(), (m.id, node)
