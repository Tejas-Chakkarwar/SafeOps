from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import safeops.experiment.runner as runner_module
from safeops.cli import EXIT_CLUSTER, EXIT_NOT_IMPLEMENTED, EXIT_USAGE, app
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
    assert (
        "First stub in sequence order: EnvironmentManager.reset_environment (Milestone 3)."
        in result.stdout
    )


# Libraries whose import could open a cluster or provider connection. The dry-run must not
# load them at all. Run in a fresh interpreter, because this test process may already
# have imported them through other tests.
_CLIENT_LIBRARIES = (
    "kubernetes",
    "prometheus_api_client",
    "anthropic",
    "openai",
    "httpx",
    "requests",
    "urllib3",
)
_DRY_RUN_PROBE = f"""
import sys
from typer.testing import CliRunner
from safeops.cli import app
result = CliRunner().invoke(app, ["run", "--dry-run"])
assert result.exit_code == 0, result.output
loaded = sorted(m for m in sys.modules if m.split(".")[0] in {set(_CLIENT_LIBRARIES)!r})
print(",".join(loaded))
"""


def test_dry_run_loads_no_cluster_or_provider_client() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _DRY_RUN_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"dry-run imported client libraries: {proc.stdout.strip()}"


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


def test_steady_state_without_harness_credential_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No harness credential: exit 5 with one line, and the admin kubeconfig is never used."""
    import yaml

    import safeops.cluster as cluster

    data = yaml.safe_load((REPO_ROOT / "config" / "cluster.yaml").read_text())
    cfg = cluster.parse_cluster_config(data, root=tmp_path)
    cfg.kubeconfig_path.parent.mkdir(parents=True)
    cfg.kubeconfig_path.write_text("admin credential that must not be used")
    monkeypatch.setattr(cluster, "load_cluster_config", lambda *a, **k: cfg)
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))

    result = cli.invoke(app, ["steady-state"])
    assert result.exit_code == EXIT_CLUSTER, result.output
    assert "never falls back to the admin kubeconfig" in result.stderr
    assert "Traceback" not in result.output
    assert called == []  # no kubectl ran at all


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_make_cluster_targets_go_through_the_bootstrap_module() -> None:
    proc = subprocess.run(
        ["make", "-n", "cluster-up", "cluster-down"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "python -m safeops.bootstrap up" in proc.stdout
    assert "python -m safeops.bootstrap down" in proc.stdout
