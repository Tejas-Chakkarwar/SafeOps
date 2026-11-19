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
- **Stubs check their call signature first.** A call that doesn't match the declared signature raises `TypeError`, exactly as a real implementation would. Without this, a wiring bug (the runner passing the wrong arguments) would hide behind "not implemented yet" until the callee's milestone. Found in the post-Milestone-1 adversarial review.
- **Runner hooks.** Wherever the runner has to interpret a stage's result, it calls one of its own stubbed hooks: discard on a dirty cluster, discard on silent non-injection, route on `proposal_status`, route on the gate decision, and so on. So when a component is implemented, the run doesn't silently ignore its result. It stops at the hook until the runner's handling is written as well.
- Numeric parameters whose values are still undecided (`verify_fault_present`'s `timeout_s`, the effectiveness timeout) are left out of the stub signatures. Each one is added at its milestone together with its own decision. No placeholder numbers.

## `safeops run --dry-run`

- **Static only:** it inspects classes, never instances, and constructs no component. It initializes no Kubernetes client, contacts no LLM provider, and has no side effects. Two tests guard this:
  - one makes component construction fail and checks that the dry-run still succeeds;
  - one runs the dry-run in a fresh interpreter and checks that no cluster or provider client library (`kubernetes`, `prometheus_api_client`, `anthropic`, `openai`, `httpx`, `requests`, `urllib3`) was imported, which also catches side effects at import time.
- **No experiment arguments:** combining it with `--fault`, `--seed` or `--trial` is a usage error.
- **Output:** it prints `RUN_SEQUENCE` (`safeops/experiment/sequence.py`). Each stage's status comes from the actual method's stub marker, so the listing reflects real implementation state. A test runs the runner with recording doubles on **all three paths**: gate-eligible and executed, gate-eligible and escalated, and not gate-eligible. On each path the call order must equal `RUN_SEQUENCE` minus the stages whose "when" label excludes them, and every call must bind to the callee's real signature. So neither the listing nor its branch labels can drift from the code.
- **Last line:** it names the *first stub in sequence order*. It doesn't claim "a real run stops here", because a stub inside a branch is reached only on runs that take that branch.
- **Exit code:** 0, because the inspection itself succeeded. The command doesn't claim to have run anything.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (including `--dry-run`) |
| 2 | usage error (Typer/Click convention): missing or invalid options |
| 3 | `NotImplementedStage`: the command reached a stub |

A separate code for "reached a stub" lets scripts tell "not built yet" apart from "called wrongly".

## Make targets

`make cluster-up` and `make cluster-down` print "not implemented until Milestone 2" and fail. The recipe exits 1, so `make` itself returns **2** (its code for a failed recipe). A target that succeeds without doing anything is the same kind of lie as a stub that returns `True`.

## Layout notes

- §13 doesn't place the workload generator. It's at `safeops/environment/workload.py`, next to the lifecycle code, since both are harness components that drive the testbed.
- Typer's pretty tracebacks are configured with `pretty_exceptions_show_locals=False`, so a crash can never dump local variables that might hold API keys or tokens.

## Notes for later milestones (from the post-Milestone-1 adversarial review)

None of these is a defect at Milestone 1. Each one lands where it says.

- **Milestones 3 and 5: post-run cleanup on mid-run failure.** `ExperimentRunner.run` has no `try/finally`. Once stages are real, an exception after `start_workload()` or `inject()` would leave the workload running and the fault in place. Whether the post-run reset always runs, and how such a run is classified (a discardable infrastructure failure, or a completed run kept with `reset_verified = false`, §11.1), is failure semantics. Decide it and log it when the lifecycle (Milestone 3) and run record (Milestone 5) exist. Don't patch it in early.
- **Milestone 4: where `Alert` and `IncidentRuntimeContext` live.** They must not be defined under `safeops.faults`. Agent, features and gate consume them, so defining them there would force those packages to import `safeops.faults`. The information-wall test would then fail, correctly. Put them in a neutral module, for example `safeops/context.py`.
- **Milestone 4: validate the fault id before using it as a path.** Today `--fault` is only printed. Once it's used to load `faults/<id>.yaml`, check it against a strict pattern (for example `^[a-z0-9][a-z0-9-]*$`) so it can't escape `faults/`.
- **Milestones 4 and 5: seed range.** `--seed` and `--trial` are non-negative but have no upper bound; `10^20` is accepted. JSON readers lose integer precision above 2^53, and some RNGs take 32- or 64-bit seeds. Decide the seed range when fault seeds start driving fault construction, and record it in the run record schema.
