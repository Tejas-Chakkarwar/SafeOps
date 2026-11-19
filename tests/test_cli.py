from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import safeops.experiment.runner as runner_module
from safeops.cli import EXIT_NOT_IMPLEMENTED, EXIT_USAGE, app
from safeops.experiment.sequence import RUN_SEQUENCE

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCEPTANCE_ARGS = ["run", "--fault", "bad-deployment-config", "--seed", "0", "--trial", "0"]

cli = CliRunner()


def test_milestone1_acceptance_command() -> None:
    """decision 0004: stops at reset_environment with one readable line, no traceback."""
    result = cli.invoke(app, ACCEPTANCE_ARGS)
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    lines = result.stderr.strip().splitlines()
    assert lines == [
        "safeops run: EnvironmentManager.reset_environment is not implemented yet "
        "(Milestone 3). Run unit: fault_id=bad-deployment-config, fault_seed=0, trial_index=0."
    ]
    assert "Traceback" not in result.output


def test_run_requires_all_experiment_arguments() -> None:
    result = cli.invoke(app, ["run"])
    assert result.exit_code == EXIT_USAGE
    assert "missing required option(s): --fault, --seed, --trial" in result.stderr


def test_run_rejects_negative_seed() -> None:
    result = cli.invoke(app, ["run", "--fault", "x", "--seed", "-1", "--trial", "0"])
    assert result.exit_code == EXIT_USAGE


def test_run_rejects_blank_fault() -> None:
    result = cli.invoke(app, ["run", "--fault", " ", "--seed", "0", "--trial", "0"])
    assert result.exit_code == EXIT_USAGE


def test_dry_run_lists_every_stage_and_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not construct or run the pipeline")

    monkeypatch.setattr(runner_module, "build_runner", forbidden)
    monkeypatch.setattr(runner_module.ExperimentRunner, "__init__", forbidden)
    result = cli.invoke(app, ["run", "--dry-run"])
    assert result.exit_code == 0, result.output
    for stage in RUN_SEQUENCE:
        assert stage.qualname in result.stdout
    assert f"{len(RUN_SEQUENCE)} stages" in result.stdout
    assert "stops first at EnvironmentManager.reset_environment (Milestone 3)" in result.stdout


@pytest.mark.parametrize("extra", [["--fault", "x"], ["--seed", "0"], ["--trial", "0"]])
def test_dry_run_takes_no_experiment_arguments(extra: list[str]) -> None:
    result = cli.invoke(app, ["run", "--dry-run", *extra])
    assert result.exit_code == EXIT_USAGE
    assert "--dry-run takes no experiment arguments" in result.stderr


@pytest.mark.parametrize(
    ("command", "stage", "milestone"),
    [
        ("replay", "replay_cohort", 13),
        ("analyze", "analyze", 15),
        ("snapshot", "EnvironmentManager.capture_snapshot", 3),
    ],
)
def test_unimplemented_commands_fail_readably(command: str, stage: str, milestone: int) -> None:
    result = cli.invoke(app, [command])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert result.stderr.strip() == (
        f"safeops {command}: {stage} is not implemented yet (Milestone {milestone})."
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
@pytest.mark.parametrize("target", ["cluster-up", "cluster-down"])
def test_unimplemented_make_targets_exit_nonzero(target: str) -> None:
    proc = subprocess.run(
        ["make", "-s", target], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert proc.returncode != 0
    assert "not implemented until Milestone 2" in proc.stderr
