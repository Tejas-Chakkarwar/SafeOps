"""SafeOps command-line interface."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import typer

from safeops.experiment.unit import MAX_FAULT_SEED, MAX_TRIAL_INDEX
from safeops.stubs import NotImplementedStage

# Exit codes (decisions 0004, 0009).
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_STEADY_STATE = 4  # no valid steady-state baseline
EXIT_CLUSTER = 5  # guard refusal, missing or expired credential, failed cluster command
EXIT_DIRTY = 6  # the environment did not verify clean (decision 0013)
EXIT_DISCARDED = 7  # the run was discarded before the agent started (decision 0015)

# pretty_exceptions_show_locals=False: a traceback must never dump local variables,
# because they may later hold API keys or tokens (CLAUDE.md, Secrets).
app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help="SafeOps: risk-aware remediation for AI incident-response agents in Kubernetes.",
)


def _fail(command: str, message: str, code: int) -> None:
    typer.echo(f"safeops {command}: {message}", err=True)
    raise typer.Exit(code)


def _run_stage(command: str, fn: Callable[[], object], context: str = "") -> None:
    """Run ``fn``. If it hits a stub, print one readable line and exit non-zero."""
    try:
        fn()
    except NotImplementedStage as exc:
        _fail(command, f"{exc}.{context}", EXIT_NOT_IMPLEMENTED)


@app.command()
def run(
    fault: Annotated[
        str | None, typer.Option("--fault", help="Fault id, e.g. bad-deployment-config.")
    ] = None,
    seed: Annotated[
        int | None,
        typer.Option("--seed", min=0, max=MAX_FAULT_SEED, help="Fault seed, 0..2^32-1."),
    ] = None,
    trial: Annotated[
        int | None,
        typer.Option(
            "--trial", min=0, max=MAX_TRIAL_INDEX, help="Trial index, 0..k-1 (k <= 1000)."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the pipeline and each stage's status. Runs nothing."),
    ] = False,
) -> None:
    """Run one experimental unit (fault_id, fault_seed, trial_index)."""
    given = {"--fault": fault, "--seed": seed, "--trial": trial}

    if dry_run:
        passed = [name for name, value in given.items() if value is not None]
        if passed:
            _fail(
                "run",
                f"--dry-run takes no experiment arguments (got {', '.join(passed)}).",
                EXIT_USAGE,
            )
        from safeops.experiment.sequence import render_dry_run

        typer.echo(render_dry_run())
        return

    missing = [name for name, value in given.items() if value is None]
    if missing:
        _fail("run", f"missing required option(s): {', '.join(missing)}.", EXIT_USAGE)
    assert fault is not None and seed is not None and trial is not None
    if not fault.strip():
        _fail("run", "--fault must not be empty.", EXIT_USAGE)

    from safeops.experiment.unit import InvalidRunUnit, RunUnit
    from safeops.faults.spec import FaultSpecError, fault_path

    try:
        unit = RunUnit(fault_id=fault, fault_seed=seed, trial_index=trial)
        if not fault_path(unit.fault_id).is_file():  # validated before it becomes a path
            raise FaultSpecError(f"no fault spec for {unit.fault_id!r} under faults/")
    except (InvalidRunUnit, FaultSpecError) as exc:
        _fail("run", f"{exc}.", EXIT_USAGE)

    from safeops.environment.workload import SteadyStateError
    from safeops.experiment.runner import PreRunDiscard, build_runner
    from safeops.faults.injector import (
        FaultInjectionError,
        FaultNotPresent,
        FaultVerificationFailed,
    )

    where = (
        f" Run unit: fault_id={unit.fault_id}, fault_seed={unit.fault_seed},"
        f" trial_index={unit.trial_index}."
    )
    try:
        _run_stage("run", lambda: build_runner().run(unit), where)
    except (PreRunDiscard, FaultNotPresent, FaultInjectionError, FaultVerificationFailed) as exc:
        _fail("run", f"discarded before the agent: {exc}.{where}", EXIT_DISCARDED)
    except SteadyStateError as exc:
        _fail("run", f"discarded, no steady-state baseline: {exc}.{where}", EXIT_STEADY_STATE)
    except FaultSpecError as exc:
        _fail("run", f"{exc}.{where}", EXIT_USAGE)
    except _cluster_errors() as exc:  # incl. WorkerQuarantined, ConfigurationError (finding 6)
        _fail("run", f"{type(exc).__name__}: {exc}.{where}", EXIT_CLUSTER)


@app.command("steady-state")
def steady_state() -> None:
    """Start the workload, measure one steady-state window, stop. Needs `make cluster-up`."""
    from safeops.cluster import CommandError, CredentialError, GuardError
    from safeops.environment.workload import (
        SteadyStateError,
        WorkloadGenerator,
        check_steady_state,
    )

    generator = WorkloadGenerator()
    s = generator.settings
    typer.echo(
        f"safeops steady-state: {s.rate_rps:g} req/s to {s.target_path}, {s.warmup_s:g}s warmup,"
        f" window of {s.window_attempts} scheduled attempts, {s.attempt_deadline_s:g}s deadline,"
        f" pass at >= {s.min_successes}"
    )
    try:
        generator.start_workload()
        try:
            result = generator.verify_steady_state()
        finally:
            generator.stop_workload()
        typer.echo(
            f"  window {result.window_id}: {result.state}, scheduled {result.scheduled}"
            f"/{result.size}, succeeded {result.succeeded}, success_rate {result.success_rate},"
            f" p95 {result.p95_latency_ms} ms"
        )
        check_steady_state(result)
    except SteadyStateError as exc:
        _fail("steady-state", str(exc), EXIT_STEADY_STATE)
    except (CredentialError, GuardError, CommandError) as exc:
        _fail("steady-state", str(exc), EXIT_CLUSTER)
    typer.echo("  baseline: PASS")


@app.command("workload-stop")
def workload_stop() -> None:
    """Stop the workload generator, e.g. after a run died without stopping it.

    Never run this while another run is using the generator (decision 0009).
    """
    from safeops.cluster import CommandError, CredentialError, GuardError
    from safeops.environment.workload import WorkloadGenerator

    try:
        WorkloadGenerator().stop_workload()
    except (CredentialError, GuardError, CommandError) as exc:
        _fail("workload-stop", str(exc), EXIT_CLUSTER)
    typer.echo("safeops workload-stop: generator stopped")


@app.command()
def replay() -> None:
    """Offline gate replay over cohort G."""
    from safeops.experiment.replay import replay_cohort

    _run_stage("replay", replay_cohort)


@app.command()
def analyze() -> None:
    """Metrics, funnel, and risk-automation curves."""
    from safeops.analysis.report import analyze as analyze_runs

    _run_stage("analyze", analyze_runs)


def _cluster_errors():
    from safeops.cluster import CommandError, CredentialError, GuardError
    from safeops.environment.manager import EnvironmentStateError

    return (CredentialError, GuardError, CommandError, EnvironmentStateError)


@app.command()
def snapshot() -> None:
    """Print the canonical snapshot of the testbed (JSON) and its digest."""
    from safeops.environment.manager import EnvironmentManager

    try:
        snap = EnvironmentManager().capture_snapshot()
    except _cluster_errors() as exc:
        _fail("snapshot", str(exc), EXIT_CLUSTER)
    typer.echo(snap.to_json())
    typer.echo(f"digest: {snap.digest()}", err=True)


def _report(command: str, result) -> None:
    if result.clean:
        typer.echo("  verify: CLEAN")
        return
    typer.echo(f"  verify: DIRTY ({len(result.problems)} problems)")
    for problem in result.problems[:25]:
        typer.echo(f"    - {problem}")
    _fail(command, "environment is not clean", EXIT_DIRTY)


@app.command()
def verify() -> None:
    """Check the testbed against config/desired_state.yaml, including node-level residue."""
    from safeops.environment.manager import EnvironmentManager

    try:
        result = EnvironmentManager().verify_clean_state()
    except _cluster_errors() as exc:
        _fail("verify", str(exc), EXIT_CLUSTER)
    _report("verify", result)


@app.command("quarantine-clear")
def quarantine_clear() -> None:
    """Lift a quarantine after fixing its cause. Only if the environment verifies clean now."""
    from safeops.environment.manager import EnvironmentManager

    try:
        result = EnvironmentManager().clear_quarantine()
    except _cluster_errors() as exc:
        _fail("quarantine-clear", str(exc), EXIT_CLUSTER)
    _report("quarantine-clear", result)
    typer.echo("safeops quarantine-clear: quarantine lifted")


@app.command()
def reset(
    times: Annotated[
        int, typer.Option("--times", min=1, help="Repeat reset -> verify N times.")
    ] = 1,
) -> None:
    """Reset the testbed to desired state, then verify it independently."""
    import time as _time

    from safeops.environment.manager import EnvironmentManager

    env = EnvironmentManager()
    for i in range(1, times + 1):
        started = _time.monotonic()
        try:
            env.reset_environment()
            result = env.verify_clean_state()
        except _cluster_errors() as exc:
            _fail("reset", f"iteration {i}: {exc}", EXIT_CLUSTER)
        typer.echo(f"safeops reset: iteration {i}/{times} in {_time.monotonic() - started:.1f}s")
        _report("reset", result)


if __name__ == "__main__":
    app()
