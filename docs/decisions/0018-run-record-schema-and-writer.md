# 0018 — Run record schema v1 and the writer

**Status:** Accepted (2026-11-19), Milestone 5
**Design refs:** §5, §7.1–7.4, §10.8, §11.1, §11.8, §15 Rules 5, 16, 17, 25; decisions 0003 F1, 0004, 0007, 0013, 0014, 0015, 0017 §1, §2, §7

## Order of work

0017 §1 was resolved first (its "resolved" section, answered by the user), and only then was the schema designed. That way v1 can represent a run that has trajectory, proposal and execution data but no measured outcome.

## Dependency: Pydantic

- **Pydantic** is declared directly as `pydantic>=2.14.3,<3`, the lowest version tested.
  - It is in §13's stack, and §11.8 names it for record validation.
  - Decision 0005 said to declare it when SafeOps first imports it, never relying on its transitive install through Typer.
- **Why not hand-written:** strict typing, `extra="forbid"`, and nested models are what Pydantic is for. The rules that decide the dataset's meaning are still hand-written and tested (`record_problems` in `safeops/experiment/schema.py`, Rule 16).
- **Import policy (0004):** Pydantic is imported only when a record is written or read, never by the dry-run. A test checks this; the dry-run denylist is unchanged.

## Schema v1 (`safeops/experiment/schema.py`)

- **`schema_version` is 1.** §11.8's example shows `5`, which echoes the design revision; this is SafeOps' first implemented record schema. Changing its structure means a new version in `SCHEMA_VERSIONS` (0017 §7).
- **Strict versions (0017 §7):**
  - A record is validated against exactly its declared version.
  - **Every key is present.** Values may be null, but a missing key is refused, so "nullable" never means "omittable".
  - Unknown keys are refused at every level.
  - Types are strict: `"1"` is not 1, and `true` is not 1.
  - An unknown or ill-typed `schema_version` is refused before any field is read.
- **Required content:** the user's required set, which is `unit`, the timestamps (`started_at`, `finalized_at`), `schema_version`, `discarded` and `discard_reason`. Three things that aren't downstream data are also required: the identity (`run_id`, a UUID4; `phase`, `A` or `do-nothing`) and `provenance`. A record without them can't be attributed, and provenance is collectable even for a dirty-pre-run discard. Everything else is nullable.
- **The fields, relative to §11.8:**
  - **§11.8's fields, kept:** `unit`, `provenance`, `clean_state_verified`, `steady_state`, `prefault_snapshot_hash`, `fault_verified`, `postfault_snapshot_hash`, `alert`, `trajectory`, `proposal_status`, `proposal`, `feature_vector`, `gate`, `shadow_gates`, `execution`, `outcome`, `totals`, `reset_verified`, `discarded`, `discard_reason`, `in_cohort_g`.
  - **Added, all already decided elsewhere:**
    - `started_at` and `finalized_at`;
    - `fault`: the variant, injection time and backend (0014, 0015);
    - `fault_verification`: the measured windows (0015);
    - `postfault_settledness` (0013, 0017 §2);
    - `stabilization_timed_out` (§5: "the timeout is recorded");
    - `provenance.fault_backend` (0003 F1);
    - `provenance.safeops_tree_dirty`, since a commit hash means nothing on a dirty tree;
    - `provenance.running_images`, the third-party digests (0007).
  - **Changed:** `discard_reason` is an object `{stage, phase, cause, error}`, not a string, so analysis can always separate pre-agent from post-agent discards (0017 §1).
  - **Deliberately absent:** a clock-offset field. That's Milestone 6's, with a version bump (0017 §6, §7).
- **Provenance fields owned by later milestones** (`provider`, `model_id`, `prompt_hash`, `tool_schema_hash`, `action_semantics_hash`, `gate_feature_version`, `evidence_requirements_hash`, `conflict_rules_hash`) are in v1 because §11.8 designs them. They are null until their producers exist. A rule then **requires** them as soon as their stage's data is present, so a trajectory without the agent's provenance is refused. These fields are designed, not placeholders: they describe data v1 must already hold for a post-agent discard.
- **Cross-field rules** (`record_problems`), each tested by a malformed-record case:
  - **Monotonic presence** along the core chain: pre-run check → steady state → pre-fault snapshot → fault → verification → post-fault snapshot → alert → trajectory → stabilization → post-action snapshot → outcome.
  - **Data that travels together:** `fault_verified` with `fault_verification`; the post-fault hash with its settledness; the trajectory with `proposal_status`; the feature vector, gate and shadow gates as a set.
  - **Branch rules:** a gate only for `valid_action`; an execution only for `execute`; no alert unless the symptom held.
  - **Provenance completeness** per stage.
  - **Discards:**
    - `discard_reason` is set exactly when `discarded` is true;
    - its phase must agree with its stage;
    - a pre-agent discard carries no agent or execution data;
    - a discarded run has no outcome (`effective` and `harmful` are unmeasured);
    - each cause has its evidence (`pre_run_dirty` needs a dirty check; `silent_non_injection` needs a verification that didn't hold; `no_steady_state` needs the failing window).
  - **A completed run** has the whole chain, `effective` and `harmful` measured, and `reset_verified`.
  - **`in_cohort_g`** must equal "not discarded, `valid_action`, `applied`" (§7.2).
  - **`category`** must follow §7.3 within G, and is null outside it. The §7.4 categories outside G are Milestone 15's.

## Writer (`safeops/experiment/records.py`)

- **Validation comes before writing, and the file is written exactly once.**
  - The record is serialized and then validated.
  - It's written to a temporary file, fsynced, and hard-linked into `data/runs/<run_id>.json`. The link fails if the record exists, so a record is never overwritten.
  - It's then made read-only (0444).
- **An invalid record is a writer bug.** It is kept under `data/runs/rejected/`, never in the dataset, and `RecordValidationError` is raised (CLI exit 9). The run is never silently lost, and the dataset never holds a malformed record.
- **Later milestones' objects** give their record form through `to_record()`. `finalize()` refuses unknown fields.
- **`ProvenanceSource`** gathers:
  - the git commit and whether the tree is dirty;
  - `kubectl version` and `kind version`, best effort: an unreadable version is null, never guessed;
  - the image digests from `.safeops/images.json`;
  - the desired-state and topology hashes, and the projection version.
  - **`is_pilot_run` defaults to true.** Every run before the Milestone 14 freeze is a pilot run (§10.8), and the campaign runner turns it off.
  - `fault_definition_hash` comes from the injected fault's spec, or from the spec file if injection never happened.

## The runner boundary (0017 §1, R1 and R2)

- **Where an attempt starts:** the pre-run check producing a result, clean or dirty. A failure that prevents any result propagates with no record: a configuration, guard or credential error, a quarantined or halted worker. A pre-run reset that raises `CommandError` is a dirty result (0013, M2), so it is recorded.
- **Pre-run dirty:** the discard record comes first, then the quarantine (the same order as post-run). `_require_pre_run_clean` is now a pure check. Its Milestone 3 test was updated to assert that order, not removed.
- **Every step names its stage** before it runs. On a failure:
  - before R2, a stub or an abort writes no record;
  - a pre-agent failure writes a discard record and re-raises (the fault is left for the next pre-run reset, 0015);
  - from the agent on: cleanup, then the record, then recovery, raised as `PostAgentDiscard` (CLI exit 8). A stub or abort under R2 re-raises its own exception after the record.
- **A second Ctrl-C during cleanup** is held until the record is written. Recovery is then skipped, and the next pre-run check finds the environment dirty.
- **The stabilization result** (`wait_stabilization_window`'s return value) is now recorded. Before, it was ignored.

## Exit codes (adds to 0004's table)

| Code | Meaning |
|---|---|
| 8 | `safeops run` was discarded after the agent started; the record holds all its data |
| 9 | the run record couldn't be written, or failed validation (kept under `data/runs/rejected/`) |

## Tests

`tests/test_m5_records.py`:
- a fabricated perfect run and a fabricated catastrophic run;
- every discard path, with its record or its deliberate absence;
- R2 under Ctrl-C and under a stub;
- the 0017 §1 harm-crash probe, which now writes a record, resets, finalizes before quarantine, and leaves the measurement gap derivable;
- 27 malformed completed records and 6 malformed discards;
- the writer's immutability, the rejected path and provenance collection.

A mutation check undid the key rules (monotonic presence, the cohort flag, pre-agent purity, forbidding extra fields, strict types, R2, the R1 exclusions, the pre-run record, no-overwrite). Each one failed a test. The cohort rule initially survived, because the category rule caught the same case; a test now isolates it.

**The dataset is protected from tests.** An autouse fixture points default writers at a temporary directory, and fails any test that changes the repository's `data/runs/`. That fixture exists because, before it, a test run wrote five fabricated records there. They were moved out to the session scratchpad, not deleted.

## Live (2026-11-19, `tests/cluster/test_records_live.py`, 4 passed)

- **The deployed load generator's run boundary (0017 §5)**, over the real API-server proxy:
  - start → measure → stop leaves the window listed;
  - the next start clears it (404 by id) and restarts `dispatched_total`;
  - a new window measures 20/20, and window ids keep increasing.
- **A real silent non-injection writes exactly one valid discard record.** Its cause is `silent_non_injection` at stage `fault_present`, phase `pre_agent`. It holds the 300/300 steady state and three measured windows, with no alert, trajectory or outcome. The provenance is real: commit, `kubectl version`, `kind version`, and the image digests from `images.json`. The run stopped its own load.
- **A real run through every harness stage**, with only the agent, gate, executor and verifiers fabricated (the executor touches nothing), writes exactly one complete record. It is in cohort G, settled in 3 polls, has `reset_verified` true and wall time 124 s, and the post-run reset left the testbed clean.
- **The acceptance command `safeops run --fault bad-deployment-config --seed 0 --trial 0`** reaches the agent stub (exit 3) and writes **no** record (R1). Its load is stopped.
- The repository's `data/runs/` was never created during any of this.
