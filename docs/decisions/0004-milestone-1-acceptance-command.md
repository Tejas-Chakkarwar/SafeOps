# 0004 — Milestone 1 acceptance command, stubs, dry-run, exit codes

**Status:** Accepted (2026-11-18)
**Design refs:** §5 (run sequence), §14 (Milestone 1), §15 Rule 15

## Acceptance command

§14 says bare `safeops run` should reach the first stub. But the real signature is `safeops run --fault <id> --seed <n> --trial <i>`, and all three options are required, so bare `safeops run` stops at argument validation. We don't invent permanent defaults to match that wording. The Milestone 1 acceptance command is:

```bash
safeops run --fault bad-deployment-config --seed 0 --trial 0
```

It must terminate at `EnvironmentManager.reset_environment()` with `NotImplementedStage`. It prints exactly one line to stderr, with no traceback, and exits non-zero. `tests/test_cli.py::test_milestone1_acceptance_command` and `tests/test_run_sequence.py::test_milestone1_first_stub_is_reset_environment` pin this. Both are scoped to Milestone 1 and get revisited when Milestone 3 implements reset.

## Stubs

- `safeops.stubs.stub(milestone=N)` replaces a function with one that raises `NotImplementedStage(stage, N)`. A stub never returns a value. Rule 15 forbids faking success (a `verify_clean_state()` that returns `True`), not refusing to run.
- **Runner hooks.** Wherever the runner has to interpret a stage's result, it calls one of its own stubbed hooks: discard on a dirty cluster, discard on silent non-injection, route on `proposal_status`, route on the gate decision, and so on. So when a component is implemented, the run doesn't silently ignore its result. It stops at the hook until the runner's handling is written as well.
- Numeric parameters whose values are still undecided (`verify_fault_present`'s `timeout_s`, the effectiveness timeout) are left out of the stub signatures. Each one is added at its milestone together with its own decision. No placeholder numbers.

## `safeops run --dry-run`

- **Static only:** it inspects classes, never instances, and constructs no component. It initializes no Kubernetes client, contacts no LLM provider, and has no side effects. A test makes construction fail and checks that the dry-run still succeeds.
- **No experiment arguments:** combining it with `--fault`, `--seed` or `--trial` is a usage error.
- **Output:** it prints `RUN_SEQUENCE` (`safeops/experiment/sequence.py`). Each stage's status comes from the actual method's stub marker, so the listing reflects real implementation state. A test runs the runner with recording doubles and checks that the call order equals `RUN_SEQUENCE`, so the listing can't drift from the code.
- **Exit code:** 0, because the inspection itself succeeded. The command doesn't claim to have run anything.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including `--dry-run`) |
| 2 | usage error (Typer/Click convention): missing or invalid options |
| 3 | `NotImplementedStage`: the command reached a stub |

A separate code for "reached a stub" lets scripts tell "not built yet" apart from "called wrongly".

## Make targets

`make cluster-up` and `make cluster-down` print "not implemented until Milestone 2" and **exit 1**. A target that succeeds without doing anything is the same kind of lie as a stub that returns `True`.

## Layout notes

- §13 doesn't place the workload generator. It's at `safeops/environment/workload.py`, next to the lifecycle code, since both are harness components that drive the testbed.
- Typer's pretty tracebacks are configured with `pretty_exceptions_show_locals=False`, so a crash can never dump local variables that might hold API keys or tokens.
