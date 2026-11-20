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
- **Required content:** the user's required set, which is `unit`, the timestamps (`started_at`, `finalized_at`), `schema_version`, `discarded` and `discard_reason`. Three things that aren't downstream data are also required: the identity (`run_id`, a UUID4; `phase`, which is **`A` only** in v1, *→ narrowed from "`A` or `do-nothing`" by the adversarial-review amendment, item 2; do-nothing runs get their own version*) and `provenance`. A record without them can't be attributed, and provenance is collectable even for a dirty-pre-run discard. Everything else is nullable.
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

## Amendment, 2026-11-19: Milestone 5 adversarial review

No v1 record existed yet, so these schema changes are fixes to v1 itself, not a v2. The regression tests are in the "Milestone 5 adversarial review" section of `tests/test_m5_records.py`. A mutation check undid each fix, and every one failed a test. The writer's NaN check survived until a test put a NaN inside an untyped tool result.

1. **`action_attempted` (new, required).**
   - **The problem:** an executor that raised mid-apply left `execution: null` with `gate.decision: execute`, so 0017 §1's measurement gap ("`execution` non-null") missed the run most likely to have changed the cluster.
   - **The fix:** the runner sets `action_attempted` *before* calling the executor.
   - **New rules:**
     - an attempt needs a gate decision to execute;
     - an execution result implies an attempt;
     - a completed run whose gate said execute must have both;
     - a pre-agent discard can't claim an attempt.
   - `schema.unmeasured_executed_action(record)` computes the gap, and 0017 §1's wording is amended to match.
2. **`phase` is `"A"` only.** A completed do-nothing run (§5: no agent) was rejected because v1's core chain requires a trajectory. Do-nothing runs (Milestone 14) get their own schema version rather than a value v1 accepts but can't represent.
3. **Provenance is captured once, at the start of the run.** Before, it was captured at `finalize`, a few minutes later, so a commit or edit during the run would have been attributed to it.
   - `RunRecordWriter.capture_provenance(unit)` is the new first stage in `RUN_SEQUENCE`. It runs before the pre-run reset, so a dirty-pre-run discard has it too.
   - `finalize` refuses to build a record without it.
   - If it can't be captured, nothing was attempted (R1).
4. **A completed run must have passed its baseline:** `state == complete`, all attempts scheduled, `succeeded ≥ min_successes`. Before, a completed record with 12/300 validated.
5. **One spec source.** The writer takes `faults_dir`, and `build_runner` wires the same `FAULTS_DIR` into the writer and the injector. A run discarded before injection now records the hash of the spec the injector would have used. Once a fault is injected, the injected spec's hash is authoritative.
6. **No NaN or infinity.** The schema refuses them in every typed field (`allow_inf_nan=False`), and the writer serializes with `allow_nan=False`, so untyped content (a tool result) can't smuggle a NaN in either. Such a record goes to `rejected/`, never into the dataset.
7. **A failed recovery doesn't hide a failed record.** The record is written, then recovery runs. A record failure never stops recovery, and if recovery also fails, the record failure is attached to it as a note. Both paths, completed and post-agent discard, share `_finalize_then_recover`.

**Found by the new tests, beyond the seven findings:**
- **The problem:** two "present together" rules rejected legitimate post-agent discards. A run that crashed in the parser (trajectory, no `proposal_status`), or between the extractor and the gate (feature vector, no gate), went to `rejected/` instead of the dataset: the very loss 0017 §1 exists to prevent.
- **The fix:** those rules are now ordered along the branch, and all-or-nothing only for a completed run.
- **The systematic test that should have existed:** a crash at **every** pre-agent and post-agent stage must produce one valid record in the dataset. Restoring the old rules fails three of those stages.

## Amendment, 2026-11-20: Milestone 5 adversarial review, second pass

These are still fixes to v1, since no record exists yet. The tests are in the "second pass" section of `tests/test_m5_records.py`. A mutation check undid each fix, and every one failed a test.

1. **A discard's stage must match the data the record carries.**
   - **The problem:** records claiming a discard at `start_workload`, `verify_fault_present` or `alert`, while carrying a steady state, a fault, a verified symptom and an alert, were accepted.
   - **The fix:** `schema.STAGE_PRODUCES` maps every stage, in run order, to the record fields it writes. A discard at stage S means S raised. So:
     - nothing from S or any later stage may be present;
     - the core data from every earlier stage must be present;
     - a branch stage's data is required only when the discard is inside the branch;
     - `action_attempted` must agree with the stage (false before `execute`, true at or after it).
   - A test pins the table to the stage lists, and the crash-at-every-stage sweep checks it against the real runner.
   - **This also split a stage.** The table showed that `steady_state` covered two calls: measuring the window (which records it) and checking it. So the baseline check is now its own stage, `steady_state_baseline`, the same way `fault_present` follows `verify_fault_present`, and `no_steady_state` must be discarded there.
2. **A completed `valid_action` run must have its frozen feature vector**, shadow gates and gate. §10.2 says the vector is frozen for every valid proposal, even under always-act, and replay depends on it. Before, such a record without them validated, and it would have silently fallen out of every replayed strategy.
3. **The pre-run dirty path** now uses the same record-then-recover helper as the other paths. A record failure no longer skips the rebuild (shown with `OSError`), and if the rebuild then fails too, the record failure is attached as a note.
4. **The trajectory's internal references.** Steps must be numbered 1 to n in order, and the proposal's evidence may cite only steps that exist. These feed `evidence_sufficiency` and `prerequisites_unverified` at Milestone 12.
5. **Durability.**
   - After the hard link, the directory is fsynced too, because the link is a directory entry. Otherwise a host crash during the destructive recovery that follows could lose a record `finalize` had reported as written.
   - Both the file and the directory also get `F_FULLFSYNC` where it exists: on macOS, a plain `fsync` leaves data in the drive's cache.

## Amendment, 2026-11-20: Milestone 5 review fix list (schema v1 invariants)

These are fixed in v1 now, because 0017 §7 makes every later structural change a version bump. Deferring them to Milestones 8–9 would split pilot and campaign data across versions. The tests are in `tests/test_m5_fixlist.py`. A mutation check restored the old behavior for each item (15 mutations), and each one made a test fail.

1. **The `run_id` lifecycle.**
   - The runner allocates `run_id` **once, at the start of `run()`**, next to `started_at` and the start-of-run provenance. Nothing regenerates it, and the writer refuses a record without it.
   - The same ID goes:
     - to the shadow gates: `ShadowGateScorer.score(features, run_id)`, the seed for `random-p` (Rule 24), which isn't implemented yet;
     - to the recovery diagnostics: the quarantine reasons name the run;
     - into the record's `run_id`;
     - into its filename, `data/runs/<run_id>.json`.
2. **The run stages are a closed enum.**
   - `RunStage` (in `safeops/enums.py`) lists every stage, and the runner names each stage only through it.
   - `PRE_AGENT_STAGES` and `POST_AGENT_STAGES` are tuples of enum members. A test checks against the enum itself that they're disjoint and together cover every member exactly once, so a member added later and left out of both fails.
   - `DiscardReason.stage` is typed `RunStage`. `Discard()` refuses an unknown stage immediately.
   - `stage_phase()` raises for an unclassified stage instead of guessing pre-agent. A guess of pre-agent would skip the post-run cleanup after a mutation may have happened; extra cleanup is cheap.
3. **Provider and executor infrastructure failures are discards.**
   - `provider_failure` is a `DiscardCause`. `proposal_status == provider_failure` requires `discarded` with cause `provider_failure`, and no proposal object. It's therefore outside cohort G and has no outcome category.
   - `execution.status == infrastructure_failure` requires an `infrastructure_failure` discard.
   - `refused_by_envelope`, `rejected_invalid_action` and `partially_applied` are real observed outcomes, kept and not discarded; the last is simply outside G.
   - Fabricated records cover every value of both enums.
4. **Phase A's online gate is always-act.**
   - `Gate` is typed `strategy: "always-act"`, `decision: "execute"`, `score: null`.
   - This holds whenever a gate is present, including on a run discarded after it executed.
   - Statuses that never reach the online gate (`no_action`, `invalid_output`, `step_limit`, `model_refusal`, `provider_failure`) carry `gate: null`.
   - Shadow strategies live only in `ShadowGates`, which has its own typed fields, so pinning `Gate` can't constrain them.
5. **The parser's output depends on its status.**
   - The parse stage always produces `proposal_status`.
   - **The proposal object, by status:**
     - `valid_action`: a proposal with an action;
     - **`no_action`: a proposal whose `recommended_action` is null**, which is the design's own representation (§11.4: "`recommended_action = None` … no mutation warranted"), so no new decision was needed;
     - `invalid_output`, `step_limit`, `model_refusal` and `provider_failure`: no proposal.
   - Stage completeness no longer demands a proposal for a later discard. Each status followed by a later failure is tested through the runner.
6. **An unknown token count stays unknown.** `totals.tokens_in` and `totals.tokens_out` are the sum over steps only if every step's count is known. Otherwise they're null, never a partial sum that would pass for a complete one. A known zero stays zero. Cost is derived at analysis time from these totals (§11.8: no `cost_usd`), so it inherits the same rule.
7. **The partial-trajectory sink, an interface contract for Milestone 9.**
   - **What's recorded now:**
     - the runner will own a mutable trajectory sink for the run and pass it into `investigate`;
     - the agent appends each completed step as it happens;
     - if `investigate` raises, the failure handling serializes whatever steps are already in the sink;
     - R2 starts with the first recorded step, so an empty sink is not R2.

     This mirrors `action_attempted`, which is set before `executor.apply()` because execution can fail mid-way.
   - **Already in v1:** a discard at stage `agent` may carry a partial trajectory with no `proposal_status` (`_PARTIAL_AT_OWN_STAGE`).
   - **Not built now:** the agent plumbing itself, which arrives with the agent.
8. **Steady-state arithmetic lives on the `SteadyState` model**, so it applies to every record that carries a window, discarded or not:
   - `scheduled ≤ size`;
   - `min_successes ≤ size`;
   - `succeeded ≤ scheduled`;
   - `size ≥ 1`;
   - a `success_rate` requires `succeeded`;
   - `success_rate == succeeded / size` (absolute tolerance 1e-9, relative 0).

   **The denominator is always `size`**, the configured attempts, never `scheduled`. A timed-out window that scheduled 150 of 300 still divides by 300 (decision 0009). The completed-run baseline *policy* check stays where it was.
9. **Write errors match what's on disk.** If the hard link succeeded but confirming durability (the directory sync) failed, the error says the record **is installed**, and the record is left in place, read-only. Only a failure before the link says it was not installed. Both stay loud.
10. **Cleanup never loses an interrupt.** Every cleanup block records interrupt-class exceptions the same way: the first one captured is kept, and later blocks can neither clear nor replace it. Ordinary cleanup failures are unchanged.

`docs/safeops-design.md` is unchanged. No field was reserved for a later milestone.

## Amendment, 2026-11-20: Milestone 5 cross-field review (derived fields vs their evidence)

**The common defect:** v1 validated a derived status or summary field independently of the evidence stored beside it. Wherever a record stores both, they must now agree. The rules are in `schema._evidence_problems`, plus the cause/stage table. The tests are in `tests/test_m5_crossfield.py`. A mutation check removed each rule (17 mutations), and every one failed a test, including one that accepted *any* held observation for `fired_at` and one that used the *last*.

1. **Execution status agrees with its steps and with the proposed action.**
   - **The rules, by status:**
     - `applied`: at least one step, every step applied, and no top-level error;
     - `partially_applied`: at least one applied step and at least one that wasn't;
     - `refused_by_envelope`: no applied step, since the envelope refuses before anything reaches Kubernetes;
     - `rejected_invalid_action` and `infrastructure_failure`: deliberately left permissive, since those paths may fail before producing the usual steps (Milestone 8).
   - **A non-composite action** has at most one step, and its operation must be the proposal's `recommended_action.type`. A record can no longer claim the agent proposed a rollback, the executor scaled, and the result was "applied".
   - **`patch_configmap`** (the bounded composite, §9.2) must be exactly `[patch_configmap, restart_deployment]` when applied or partially applied. Its other statuses may hold only a prefix of that sequence.
   - **Consequence:** partial application exists only for the composite, which is what §7.1 says ("composite action, some steps succeeded").
2. **A discard's cause agrees with its stage** (`schema.CAUSE_STAGE`, one table):
   - **Causes tied to one stage:**

     | Cause | Stage |
     |---|---|
     | `pre_run_dirty` | `pre_run_clean_state` |
     | `workload_generator_busy` | `start_workload` |
     | `no_steady_state` | `steady_state_baseline` |
     | `injection_failed` | `inject` |
     | `detection_unmeasurable` | `verify_fault_present` |
     | `silent_non_injection` | `fault_present` |
     | `provider_failure` | `route_proposal` |
   - **`infrastructure_failure`** stays broad, allowed at any stage except `pre_run_clean_state`, where a failed check is always `pre_run_dirty`.
   - **`operator_abort` and `not_implemented`** are allowed only under R2: post-agent, after a recorded trajectory step or an attempted action. Before R2 they produce no record at all.
   - **Why `route_proposal` for `provider_failure`, chosen to match the Milestone 9 producer:**
     - after retrying transient provider errors 3 times (§7.1), the agent *returns* normally, with the trajectory so far and an output the parser classifies as `PROVIDER_FAILURE`;
     - the runner then discards the run when it routes the proposal.

     The agent does not raise for this. A raise would be `infrastructure_failure` at stage `agent`.
3. **`FaultVerification.present == any(observation.held)`**, in both directions. Each observation's own numbers must agree as well:
   - `server_error_responses ≤ attempts`;
   - `value == server_error_responses / attempts`;
   - `held == (value > threshold)`, with the threshold parsed from the stored symptom expression exactly as the injector parses it.
4. **The alert traces exactly to what justified it.**
   - Its `alert_name`, `target_service` and `symptom` must equal the verified symptom's alert name, target and expression.
   - `fired_at` must equal the `observed_at` of the **first** held observation in stored order. That is the producer's semantics (`FaultVerification.held_observation`), not "any held observation".
   - With item 3, alert provenance is closed end to end: measurement → held window → verified symptom → the alert's name, target, expression and time → what the agent receives.
5. **The sweep: other stored summaries with their evidence beside them.** Each producer was checked first.
   - **Pinned:**
     - **settledness:** `settled == true` ⇒ `last_changes == []`. The producer returns a settled snapshot with no changes.
     - **token totals:** `totals.tokens_in` and `totals.tokens_out` equal the null-preserving sums over the stored trajectory steps, and are null without a trajectory.
     - **wall time:** `totals.wall_seconds` equals `finalized_at − started_at`.
     - **confidence:** the `confidence` shadow scalar equals the proposal's confidence (§11.8: "confidence to the self-reported `confidence`"), and `confidence_deficit` equals `1 − confidence` (§11.5).
     - **harm:** when `harm_violations` is recorded, `harmful` is true exactly when there is at least one violation (§11.7: "at least one violation").
     - **the filename:** a record whose filename isn't its `run_id` is refused when read (`load_record`).
   - **Deliberately left unconstrained**, with the reason for each:
     - **`settled == false` ⇒ a non-empty `last_changes`:** not guaranteed. If the timeout is shorter than one poll interval, the producer times out before any comparison and reports none. A test pins that this is allowed.
     - **`settled == true` ⇒ at least 3 polls:** the required count (`SETTLE_IDENTICAL_POLLS`) isn't stored in the record.
     - **`irreversibility = 1 − reversibility[action]`, `persistence_risk` and `blast_radius`:** derived from `config/action_semantics.yaml` and the topology, which arrive with the extractor (Milestones 8 and 12). Copying those tables into the schema now would let them drift. They're to be added once those files exist.
     - **`trajectory[].result_digest` against `result`:** the canonical serialization the digest is taken over is defined only when the tool layer exists (Milestones 6 and 9). Pin it then.
     - **`stabilization_timed_out`:** there's no stored evidence to check it against.

### Integration contracts for Milestones 8 and 9 (recorded, not implemented)

- **`provider_failure` (Milestone 9).** When the parser returns `ProposalStatus.PROVIDER_FAILURE`, routing terminates the run as a provider-failure discard at `route_proposal`, with the trajectory preserved. It must never flow down the `no_action` path (stabilization, then effectiveness, then harm), which would score a provider outage as an agent outcome.
- **Executor `infrastructure_failure` (Milestone 8).** A returned `ExecutionStatus.INFRASTRUCTURE_FAILURE` becomes an infrastructure-failure discard **at once**, at stage `executed_action`. The runner must not continue into stabilization, effectiveness and harm, and discover the problem only when the final record fails validation.

## Amendment, 2026-11-20: Milestone 5 final review

The tests are in the "final review" section of `tests/test_m5_crossfield.py`. A mutation check removed each fix, and every one failed a test.

1. **`reset_verified` follows the discard phase.** The post-run cleanup runs only after the agent starts. So a pre-agent discard has `reset_verified: null` (none ran), and a post-agent discard, like a completed run, records `true` or `false`. Before, a pre-agent discard claiming a verified reset was accepted, and so was a post-agent discard with no verdict. Either would have skewed reset reliability as computed from records.
2. **No applied step after one that wasn't applied.** Steps run in order and stop at the first failure: the composite restarts only after its patch succeeded (§9.2). Before, "patch failed, then restart applied" passed as `partially_applied`.
3. **The schema holds the writer's bounds:** `postfault_settledness.last_changes` has at most `MAX_LAST_CHANGES` (50) entries, and `discard_reason.error` at most `MAX_DISCARD_ERROR` (2000) characters. One constant serves both the writer and the schema. Before, 5,000 changes and a 100 kB error validated.
4. **The CLI's wording.** Exit 9 now reads "run record problem: …", so it no longer says "not written" about a record that is installed with only its durability unconfirmed. The exception's own message carries which of the two happened.

**Deliberately unconstrained: the order of the timestamps.** (`started_at` ≤ `injected_at` ≤ `observed_at` ≤ `fired_at` ≤ `finalized_at`)
- These are wall-clock times, and a wall clock can step backwards (an NTP correction, sleep and resume).
- A hard ordering rule would reject a run that crossed such a step, which moves it to `rejected/` and out of the dataset.
- Those are exactly the runs decision 0017 §6 wants kept and flagged, through the two-reading host and cluster clock comparison at Milestones 6 and 12.
- So ordering is a Milestone 12 flag, never a v1 validation rule.

## Amendment, 2026-11-20: Milestone 5 closeout

These are the last schema and writer fixes before Milestone 5 closes. Runner control flow and Kubernetes behavior are unchanged. The tests are in `tests/test_m5_closeout.py`. A mutation check removed each rule (11 mutations), and every one failed a test. One survived at first, because an older rule caught the same case, and its test was made self-consistent until only the new rule could reject it.

1. **Timestamps.**
   - **Wall-clock ordering is not a validation rule.** The `finalized_at < started_at` rejection is gone. A run that crosses a backwards wall-clock step stays in the dataset, so decision 0017 §6's clock analysis can flag it. `started_at` and `finalized_at` remain ordinary wall-clock timestamps, and a backwards step is derivable from them, so there's no extra field for it. This is beside the earlier "timestamp ordering deliberately unconstrained" entry.
   - **`wall_seconds` is elapsed real time from a monotonic clock.** The runner samples `time.monotonic()` once at run start, next to `started_at` and `run_id`, and again when the record is finalized. It passes only the difference to the writer.
     - The raw samples are harness-internal state: not in schema v1, not in `RECORD_FIELDS`, never serialized.
     - `wall_seconds` keeps its name, because it already meant elapsed real time, as opposed to CPU or model time.
     - It is not cross-checked against `finalized_at − started_at`, and it is never clamped. A negative monotonic duration means the clock source is broken, so it fails loudly instead of being recorded as `0.0`.
2. **Strict JSON.** `validate_record_json` parses with `parse_constant` set to refuse `NaN`, `Infinity` and `-Infinity` before model validation. Python's parser accepts them, and Pydantic's `allow_inf_nan` doesn't reach `Any` payloads (`trajectory[].result`, `provenance.model_parameters`). They are refused as malformed, never converted.
3. **`SteadyState.state` has a fixed field shape** on the model, so it applies to every record that carries a window, matching `workload.py` and decision 0009:
   - **`complete`:** `scheduled == size`, `succeeded` and `success_rate` non-null, `failure` null.
   - **`failed` and `timeout`:** `succeeded`, `success_rate` and `p95_latency_ms` null, and `failure` non-null. An unmeasured window carries no success metrics.

   The fixed-denominator rule (`success_rate == succeeded / size`) is unchanged. The baseline threshold remains a completed-run policy rule, not a model rule.
4. **`started_at` is required, and the writer never fabricates it.** A missing `started_at` raises "run record requires started_at captured at run start". `finalized_at` is never substituted. `wall_seconds` is required the same way. The wall timestamps and the elapsed duration are independent pieces of run state.
5. **A measured `harmful` on a completed run carries its evidence.** `harm_violations` must be non-null whenever `harmful` is measured on a completed run, and `harmful` must be true exactly when that list is non-empty (§11.7: "at least one violation"). So the safety headline number can be audited from the immutable record. Discarded runs, which have no outcome, aren't affected. Nothing equivalent applies to `effective`, which isn't defined by an evidence list.
