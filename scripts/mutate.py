#!/usr/bin/env python3
"""Reproducible mutation checks for SafeOps. Repository-development tooling, standard library only.

    make mutate                                  # every mutant in scripts/mutants.toml
    python scripts/mutate.py --only ID [ID ...]  # some of them
    python scripts/mutate.py --check             # validate the table and its targets only

The mutants are data, in scripts/mutants.toml: each names its target file, the exact text it
replaces, the replacement, the guard it breaks, and the test node(s) that must kill it. This
file is only the runner. It is not part of the SafeOps package, the experiment, or any run's
provenance.

Fail-closed, like scripts/check_repo_policy.py. A result the harness can't establish is an
error, never a kill:
- The working tree must be clean before anything is touched (tracked and untracked). The
  harness never resets, stashes, cleans or checks out; it only restores bytes it changed.
- Every mutant's `old` text must occur exactly once in its target. Zero or several
  occurrences fail the whole run before any file is mutated: a stale mutant is never
  skipped or re-pointed.
- After writing a mutant, the file on disk must be exactly the original with `old` replaced
  by `new` at its one occurrence, and (for Python) must compile, before any test runs.
- The original bytes and their SHA-256 are captured first. They are written back in a
  `finally` (after a kill, a survival, a timeout, a failed subprocess, an assertion,
  Ctrl-C or SIGTERM) and the hash is re-verified. If restoration can't be proven, the run
  stops there.
- A mutant is killed only when pytest ran its named nodes and at least one of them failed
  on an assertion. A missing node, a collection or usage error, an error in setup or
  teardown, an import, syntax or name error, a timeout, or a skip is a harness error.
- Afterwards every target's hash and `git status` must equal their starting state.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLE_PATH = Path(__file__).resolve().with_name("mutants.toml")
TEST_TIMEOUT_S = 600

# A failure whose message starts with one of these is the mutation breaking the program,
# not the named test observing the broken invariant.
HARNESS_FAILURE_PREFIXES = (
    "SyntaxError",
    "IndentationError",
    "ImportError",
    "ModuleNotFoundError",
    "NameError",
)

Git = Callable[[list[str]], str]


class HarnessError(Exception):
    """The harness couldn't establish a result. Reported as a failure, never as a kill."""


class RestoreError(HarnessError):
    """A target's original bytes couldn't be proven restored. The run stops."""


# --- the table -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Mutant:
    id: str
    source: str
    target: str
    guard: str
    kills: tuple[str, ...]
    old: str
    new: str


@dataclass(frozen=True)
class Unreconstructible:
    id: str
    source: str
    reason: str


@dataclass(frozen=True)
class Table:
    recorded: dict[str, int]
    mutants: tuple[Mutant, ...]
    unreconstructible: tuple[Unreconstructible, ...]


def _field(entry: dict, key: str, kind: type, where: str):
    value = entry.get(key)
    if not isinstance(value, kind):
        raise HarnessError(f"{where}: {key!r} must be a {kind.__name__}, got {value!r}")
    return value


def load_table(text: str) -> Table:
    """Parse and validate the mutation table. Every structural problem is a HarnessError."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HarnessError(f"mutation table is not valid TOML: {exc}") from exc
    extra = set(data) - {"recorded", "mutant", "unreconstructible"}
    if extra:
        raise HarnessError(f"mutation table: unknown top-level keys {sorted(extra)}")
    recorded: dict[str, int] = {}
    for i, entry in enumerate(data.get("recorded", [])):
        where = f"recorded[{i}]"
        source = _field(entry, "source", str, where)
        count = entry.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise HarnessError(f"{where}: count must be a non-negative integer")
        if source in recorded:
            raise HarnessError(f"{where}: source {source!r} listed twice")
        recorded[source] = count
    mutants = []
    for i, entry in enumerate(data.get("mutant", [])):
        where = f"mutant[{i}]"
        if set(entry) != {"id", "source", "target", "guard", "kills", "old", "new"}:
            raise HarnessError(
                f"{where}: fields must be id, source, target, guard, kills, old, new"
            )
        kills = _field(entry, "kills", list, where)
        if not kills or not all(isinstance(k, str) and "::" in k for k in kills):
            raise HarnessError(f"{where}: kills must be a non-empty list of pytest node ids")
        target = _field(entry, "target", str, where)
        pure = PurePosixPath(target)
        if pure.is_absolute() or ".." in pure.parts or not target:
            raise HarnessError(f"{where}: target must be a relative path inside the repository")
        m = Mutant(
            id=_field(entry, "id", str, where),
            source=_field(entry, "source", str, where),
            target=target,
            guard=_field(entry, "guard", str, where),
            kills=tuple(kills),
            old=_field(entry, "old", str, where),
            new=_field(entry, "new", str, where),
        )
        if not m.old or m.old == m.new:
            raise HarnessError(f"mutant {m.id}: old must be non-empty and differ from new")
        mutants.append(m)
    lost = [
        Unreconstructible(
            id=_field(e, "id", str, f"unreconstructible[{i}]"),
            source=_field(e, "source", str, f"unreconstructible[{i}]"),
            reason=_field(e, "reason", str, f"unreconstructible[{i}]"),
        )
        for i, e in enumerate(data.get("unreconstructible", []))
    ]
    ids = [m.id for m in mutants] + [u.id for u in lost]
    if len(ids) != len(set(ids)):
        raise HarnessError(f"duplicate mutant ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    table = Table(recorded, tuple(mutants), tuple(lost))
    problems = recovery_problems(table)
    if problems:
        raise HarnessError("; ".join(problems))
    return table


def recovery_problems(table: Table) -> list[str]:
    """recorded == ported + unreconstructible, per recorded source and in total. Mutants
    added later name a source outside the decision records and aren't counted as recovered."""
    problems = []
    for source, count in table.recorded.items():
        ported = sum(m.source == source for m in table.mutants)
        lost = sum(u.source == source for u in table.unreconstructible)
        if ported + lost != count:
            problems.append(
                f"{source}: recorded {count}, but {ported} ported + {lost} unreconstructible"
            )
    for u in table.unreconstructible:
        if u.source not in table.recorded:
            problems.append(f"unreconstructible {u.id} names unrecorded source {u.source!r}")
    return problems


def recovery_counts(table: Table) -> tuple[int, int, int]:
    recorded = sum(table.recorded.values())
    ported = sum(m.source in table.recorded for m in table.mutants)
    return recorded, ported, len(table.unreconstructible)


# --- applying and restoring ----------------------------------------------------------------------


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_mutation(text: str, m: Mutant) -> str:
    """The mutated text. `old` must occur exactly once: never skipped, never re-pointed."""
    count = text.count(m.old)
    if count != 1:
        state = "is missing from" if count == 0 else f"occurs {count} times in"
        raise HarnessError(f"mutant {m.id}: its target text {state} {m.target} (stale mutant)")
    return text.replace(m.old, m.new, 1)


def prove_applied(original: str, on_disk: str, m: Mutant) -> None:
    """The file now holds exactly the original with `old` replaced by `new` at its one
    occurrence: the span that held `old` holds `new`, and nothing else changed."""
    at = original.find(m.old)
    expected = original[:at] + m.new + original[at + len(m.old) :]
    if at < 0 or on_disk == original or on_disk != expected:
        raise HarnessError(f"mutant {m.id}: the mutation is not in {m.target} as written")
    if on_disk[at : at + len(m.new)] != m.new:
        raise HarnessError(f"mutant {m.id}: the replacement is not at the target in {m.target}")


def restore(path: Path, original: bytes, digest: str) -> None:
    path.write_bytes(original)
    if sha256(path.read_bytes()) != digest:
        raise RestoreError(f"{path}: original bytes could not be restored (hash differs)")


# --- running the named tests and judging the result ---------------------------------------------


@dataclass(frozen=True)
class Case:
    nodeid: str
    outcome: str  # passed | failed | error | skipped
    message: str


@dataclass(frozen=True)
class TestRun:
    returncode: int | None  # None: timed out
    junit: str | None
    output: str


@dataclass(frozen=True)
class Verdict:
    result: str  # killed | survived | error
    detail: str
    killer: str | None = None


def junit_nodeid(root: Path, classname: str, name: str) -> str:
    """pytest's node id from a JUnit testcase: classname is the module path, dotted, then any
    test class names. A collection error has an empty classname: there is no module to
    rebuild, so it keeps the fallback form, and classify() still sees its error."""
    if not classname:
        return f"{classname}::{name}"
    parts = classname.split(".")
    for k in range(len(parts), 0, -1):
        module = PurePosixPath(*parts[:k]).with_suffix(".py")
        if (root / module).is_file():
            return "::".join([module.as_posix(), *parts[k:], name])
    return f"{classname}::{name}"


def parse_junit(text: str, root: Path) -> list[Case]:
    try:
        tree = ET.fromstring(text)
    except ET.ParseError as exc:
        raise HarnessError(f"pytest's JUnit report is unreadable: {exc}") from exc
    cases = []
    for tc in tree.iter("testcase"):
        nodeid = junit_nodeid(root, tc.get("classname", ""), tc.get("name", ""))
        outcome, message = "passed", ""
        for tag in ("error", "failure", "skipped"):
            el = tc.find(tag)
            if el is not None:
                outcome = {"error": "error", "failure": "failed", "skipped": "skipped"}[tag]
                message = el.get("message") or (el.text or "").strip()
                break
        cases.append(Case(nodeid, outcome, message))
    return cases


def _matches(case: Case, node: str) -> bool:
    return case.nodeid == node or case.nodeid.startswith(node + "[")


def classify(m: Mutant, run: TestRun, cases: list[Case]) -> Verdict:
    """Killed only if a named node ran and failed on an assertion. Everything else that
    isn't a clean pass of every named node is a harness error."""
    if run.returncode is None:
        return Verdict("error", f"pytest timed out after {TEST_TIMEOUT_S}s")
    if run.returncode not in (0, 1):
        return Verdict(
            "error", f"pytest exit {run.returncode} (collection, usage or internal error)"
        )
    errors = [c for c in cases if c.outcome == "error"]
    if errors:
        return Verdict(
            "error", f"{errors[0].nodeid}: error outside the test body: {errors[0].message[:200]}"
        )
    named = []
    for node in m.kills:
        ran = [c for c in cases if _matches(c, node)]
        if not ran:
            return Verdict("error", f"named test {node} was not executed")
        if all(c.outcome == "skipped" for c in ran):
            return Verdict("error", f"named test {node} was skipped")
        named.extend(ran)
    failed = [c for c in named if c.outcome == "failed"]
    broken = [c for c in failed if c.message.startswith(HARNESS_FAILURE_PREFIXES)]
    if broken:
        return Verdict(
            "error", f"{broken[0].nodeid}: the mutant broke the program: {broken[0].message[:200]}"
        )
    if failed:
        return Verdict(
            "killed",
            failed[0].message.splitlines()[0][:200] if failed[0].message else "",
            failed[0].nodeid,
        )
    if run.returncode != 0:
        return Verdict("error", "pytest reported a failure outside the named tests")
    return Verdict("survived", "every named test passed with the mutant in place")


TestRunner = Callable[[Path, tuple[str, ...]], TestRun]


def run_pytest(root: Path, nodes: tuple[str, ...]) -> TestRun:
    """Run exactly the named nodes. No bytecode is written, so a restored file can never be
    shadowed by a cached compile of its mutant (same size, same mtime second)."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryDirectory(prefix="safeops-mutate-") as tmp:
        report = Path(tmp) / "junit.xml"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            f"--junitxml={report}",
            *nodes,
        ]
        try:
            # Bytes, decoded leniently: a mutant can make pytest print invalid UTF-8 (found
            # porting 0023-SB, surrogateescape), and that must never crash the harness.
            proc = subprocess.run(
                cmd, cwd=root, capture_output=True, timeout=TEST_TIMEOUT_S, env=env, check=False
            )
        except subprocess.TimeoutExpired as exc:
            return TestRun(None, None, _text(exc.stdout))
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError(f"pytest could not run: {exc}") from exc
        junit = _text(report.read_bytes()) if report.exists() else None
        return TestRun(proc.returncode, junit, _text(proc.stdout) + _text(proc.stderr))


def _text(data: bytes | str | None) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def run_mutant(root: Path, m: Mutant, runner: TestRunner) -> Verdict:
    path = root / m.target
    original = path.read_bytes()
    digest = sha256(original)
    text = original.decode("utf-8")
    mutated = apply_mutation(text, m)  # raises before anything is written
    try:
        path.write_bytes(mutated.encode("utf-8"))
        prove_applied(text, path.read_bytes().decode("utf-8"), m)
        if m.target.endswith(".py"):
            try:
                compile(mutated, str(path), "exec")
            except SyntaxError as exc:
                raise HarnessError(f"mutant {m.id} is not valid Python: {exc}") from exc
        run = runner(root, m.kills)
    finally:
        restore(path, original, digest)
    if run.junit is None:
        if run.returncode is None:
            return Verdict("error", f"pytest timed out after {TEST_TIMEOUT_S}s")
        return Verdict(
            "error", f"pytest wrote no report (exit {run.returncode}): {run.output[-300:]}"
        )
    return classify(m, run, parse_junit(run.junit, root))


# --- the repository's state ----------------------------------------------------------------------


def make_git(root: Path) -> Git:
    def git(args: list[str]) -> str:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError(f"git {' '.join(args)} could not run: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            raise HarnessError(
                f"git {' '.join(args)} failed: {detail[-1] if detail else proc.returncode}"
            )
        return proc.stdout

    return git


def tree_state(git: Git) -> str:
    """Tracked changes and untracked files, exactly as Git reports them."""
    return git(["status", "--porcelain=v1", "-z", "--untracked-files=all"])


def stale_targets(root: Path, mutants: Iterable[Mutant]) -> list[str]:
    problems = []
    for m in mutants:
        path = root / m.target
        if not path.is_file():
            problems.append(f"mutant {m.id}: target {m.target} does not exist")
            continue
        try:
            apply_mutation(path.read_bytes().decode("utf-8"), m)
        except HarnessError as exc:
            problems.append(str(exc))
    return problems


def _hashes(root: Path, mutants: Iterable[Mutant]) -> dict[str, str]:
    return {m.target: sha256((root / m.target).read_bytes()) for m in mutants}


def run(
    root: Path,
    table: Table,
    *,
    only: Iterable[str] = (),
    check_only: bool = False,
    git: Git | None = None,
    runner: TestRunner = run_pytest,
    out: Callable[[str], None] = print,
) -> int:
    git = git or make_git(root)
    wanted = list(only)
    unknown = sorted(set(wanted) - {m.id for m in table.mutants})
    if unknown:
        out(f"FAIL unknown mutant ids {unknown}")
        return 1
    selected = [m for m in table.mutants if not wanted or m.id in wanted]
    stale = stale_targets(root, selected)
    if stale:
        out("FAIL stale mutants (nothing was mutated):")
        for problem in stale:
            out(f"  - {problem}")
        return 1
    if check_only:
        out(f"ok   {len(selected)} mutants: every target matches exactly once")
        _report_recovery(table, out)
        return 0

    initial = tree_state(git)
    if initial:
        out("FAIL the working tree is not clean; commit or remove your changes first.")
        out("     (the harness never resets, stashes or cleans, and won't mutate on top of edits)")
        return 1
    hashes = _hashes(root, selected)
    verdicts: list[tuple[Mutant, Verdict]] = []
    aborted = None
    for m in selected:
        try:
            verdict = run_mutant(root, m, runner)
        except RestoreError as exc:
            aborted = str(exc)
            verdicts.append((m, Verdict("error", aborted)))
            break
        except HarnessError as exc:
            verdict = Verdict("error", str(exc))
        verdicts.append((m, verdict))
        changed = [t for t, h in _hashes(root, selected).items() if h != hashes[t]]
        if changed:
            aborted = f"after mutant {m.id}, targets differ from their originals: {changed}"
            break
        out(_line(m, verdict))

    final = []
    if any(sha256((root / t).read_bytes()) != h for t, h in hashes.items()):
        final.append("a target's bytes differ from its original")
    if tree_state(git) != initial:
        final.append("the working tree differs from its starting state")
    _summary(verdicts, out)
    _report_recovery(table, out)
    if aborted:
        out(f"FAIL aborted: {aborted}")
    for problem in final:
        out(f"FAIL {problem}")
    ok = not aborted and not final and len(verdicts) == len(selected)
    ok = ok and all(v.result == "killed" for _, v in verdicts)
    out("ok   every mutant killed; tree restored" if ok else "FAIL mutation checks")
    return 0 if ok else 1


def _line(m: Mutant, v: Verdict) -> str:
    label = {"killed": "KILLED  ", "survived": "SURVIVED", "error": "ERROR   "}[v.result]
    tail = f"<- {v.killer}" if v.killer else v.detail
    return f"{label} {m.id:<9} {m.target}  [{m.guard}]  {tail}"


def _summary(verdicts: list[tuple[Mutant, Verdict]], out: Callable[[str], None]) -> None:
    out("")
    out(f"{'id':<9} {'result':<8} {'target':<36} guard / killing test")
    for m, v in verdicts:
        out(f"{m.id:<9} {v.result:<8} {m.target:<36} {m.guard}")
        out(f"{'':<9} {'':<8} {'':<36} {v.killer or v.detail}")
    counts = {r: sum(v.result == r for _, v in verdicts) for r in ("killed", "survived", "error")}
    out(
        f"\n{len(verdicts)} run: {counts['killed']} killed, {counts['survived']} survived,"
        f" {counts['error']} harness errors"
    )


def _report_recovery(table: Table, out: Callable[[str], None]) -> None:
    recorded, ported, lost = recovery_counts(table)
    out(
        f"historical recovery (decisions 0022-0024): recorded {recorded}, ported {ported},"
        f" unreconstructible {lost}"
    )
    for u in table.unreconstructible:
        out(f"  unreconstructible {u.id} ({u.source}): {u.reason}")


def _sigterm(signum, frame):  # let every `finally` restore its file
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only", nargs="+", default=[], metavar="ID", help="run only these mutants"
    )
    parser.add_argument("--check", action="store_true", help="validate the table and targets only")
    args = parser.parse_args(argv)
    try:
        table = load_table(TABLE_PATH.read_text())
    except (OSError, HarnessError) as exc:
        print(f"FAIL {exc}")
        return 1
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        return run(REPO_ROOT, table, only=args.only, check_only=args.check)
    except KeyboardInterrupt:
        print("FAIL interrupted (the mutated file, if any, was restored)")
        return 130
    except HarnessError as exc:
        print(f"FAIL {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
