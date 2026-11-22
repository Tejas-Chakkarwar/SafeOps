# 0022 — Reset mutation boundary, exact cluster ownership, campaign image lock

**Status:** Accepted (2026-11-22), between Milestones 5 and 6. A focused hardening pass: Milestone 5 stays closed and Milestone 6 is current. No run-record schema change.
**Design refs:** §5 (run sequence), §10.8 (freeze discipline), §11.1 (lifecycle, quarantine), §11.8 (provenance), §15 Rules 8, 22; decisions 0006, 0012, 0013, 0017 §1, 0018, 0021

Three items, in priority order. The first is a correctness bug in the run lifecycle. The second closes a narrower gap in destructive teardown. The third specifies a future requirement now, so it can't be built wrong later.

## 1. Reset's mutation boundary

### The defect

The runner's pre-run step was:

```python
try:
    env.reset_environment()
    pre_clean = env.verify_clean_state()
except CommandError as exc:
    pre_clean = CleanStateResult(False, ...)  # dirty -> pre_run_dirty discard
```

Its comment said a configuration, guard or credential error "propagates: nothing was attempted, so no record". That is false once reset has begun mutating.
- `reset_environment()` isn't atomic: it deletes Chaos objects and strays, deletes and recreates the app tier, replaces the StatefulSet, then waits.
- `CredentialError` and `GuardError` aren't `CommandError`s. A `subprocess.TimeoutExpired` from a hung kubectl isn't either.
- Any of them, three deletes and two creates into a reset, propagated as a bare exception: **no record, no quarantine, and a half-reset worker** the next unit would run on.
- The same held after a successful reset, for `verify_clean_state()`, which re-raises guard and credential errors by design (it can't classify them as dirt).

### The boundary, enforced by type

- **Where it is:** reset's **first mutating call**. `reset_environment()` routes every delete, create and replace through `_ResetProgress.mutate`, which records the step name and sets `mutated` *before* the call, because a call that raises may still have applied. A timeout never proves the API server didn't apply it.
- **Before it** (preflight), failures propagate as themselves; nothing was changed:
  - the quarantine refusal (`WorkerQuarantined`);
  - `_configuration()`: a stale desired state, invalid manifests or topology, or images that don't match what cluster-up loaded (`ConfigurationError`);
  - building the client, and the reads before the first mutation (for example listing Chaos objects).

  None of these checks moved.
- **After it,** any ordinary failure (`Exception`, not `BaseException`) raises **`ResetMutationError(EnvironmentStateError)`**:
  - `step` names the operation that failed: `delete Deployment/orders`, `create Service objects`, `list projected objects`, `wait for the stabilization window`;
  - the original exception is `__cause__`;
  - the message carries the underlying error bounded to `MAX_RESET_ERROR` = 500 characters, with its original length noted.

  It lives with the other lifecycle errors, so the CLI's existing `EnvironmentStateError` handling covers `safeops reset` too.
- **Interrupts are never wrapped.** `KeyboardInterrupt` and `SystemExit` propagate unchanged. In the pre-run step that remains an operator abort with no record (R1), as before. The next run's pre-run reset finds whatever was left.

### The runner: a conversion, not a new lifecycle

`ExperimentRunner._pre_run_clean_state()` turns the two post-boundary failures into a not-clean `CleanStateResult`:

| Failure | Result problem |
|---|---|
| `CommandError` from reset (a read failing outright before mutation; unchanged, review M2) | `pre-run reset failed: …` |
| `ResetMutationError` | `pre-run reset failed: reset failed after it began mutating the testbed, at <step>: <Type>: …` |
| Any `Exception` from `verify_clean_state()` after a reset returned | `pre-run clean-state verification failed after reset: <Type>: …` (bounded to 500 characters) |

- **One canonical path.** That result then takes the existing path: `_require_pre_run_clean` → `PreRunDiscard` → `_finalize_then_recover`, which writes the record first and quarantines second. That's the same path an ordinary dirty verification takes.
- **No second writer or recovery handler.** The conversion writes no record, raises no discard, and never recovers. A structural test pins exactly one `records.finalize(` call, two `_quarantine(` call sites and three `_finalize_then_recover(` call sites.
- **Recorded values:** cause **`pre_run_dirty`**, stage **`pre_run_clean_state`**, **`clean_state_verified = false`**. The failing operation is in the bounded `discard_reason.error`, and in the quarantine reason.
  - That's exactly true: the environment couldn't be established clean before the run.
  - `infrastructure_failure` would be wrong, and the Milestone 5 validator already forbids it (a failed pre-run check is `pre_run_dirty`).
  - There's no reset-specific stage, and none is needed. **The schema is unchanged.**
- **Inherited behavior, unchanged:**
  - a record that can't be written still doesn't stop recovery, and still surfaces after it;
  - a failed quarantine raises `QuarantineFailed`, halts the worker, and refuses the next unit before it touches anything;
  - exactly one record is written, not one per nested handler.

### Tests and mutation checks

`tests/test_reset_boundary.py`, 31 tests:
- **Manager:**
  - credential, guard, command, timeout and runtime failures after earlier mutations applied → `ResetMutationError`, with the cause preserved and the step named;
  - a read after the boundary is named as the failing step;
  - the first mutation failing is already past the boundary;
  - preflight failures (configuration, quarantine, a read before any mutation) propagate unwrapped, with zero mutations;
  - interrupts are unwrapped;
  - the error is bounded.
- **Runner:**
  - the three paths (dirty result, `ResetMutationError`, verification failure) each produce the identical call sequence `require_pre_run_clean → finalize_then_recover → finalize → quarantine`, exactly one record, and the values above;
  - the same holds when the quarantine fails, and when the record write fails;
  - the defect end-to-end through the real `EnvironmentManager`: three mutations apply, the fourth hits an expired credential, and the run is recorded, then quarantined;
  - preflight writes nothing;
  - interrupts are unchanged;
  - the structural single-path test.

**Mutation checks: 6, all killed:**
- removing the wrapper (9 tests fail);
- giving `ResetMutationError` its own quarantine-and-raise handler (5);
- removing the pre-run quarantine (16);
- classifying as `infrastructure_failure` (18 failures, 8 errors);
- letting `ResetMutationError` propagate (4);
- letting a verification failure propagate (4).

## 2. Exact cluster ownership for teardown

### What labels prove

Decision 0021 made teardown check the kind labels on `<cluster>-control-plane`: `io.x-k8s.kind.cluster=safeops`, `io.x-k8s.kind.role=control-plane`.
- That proves a kind cluster *named* safeops, nothing more.
- A same-name cluster recreated by anything later gets the same labels and the same container name.
- On a solo machine the window is narrow, but quarantine runs teardown unattended, and pinning is cheap.

### The pin

- Right after its own successful `kind create`, before any other step, `cluster-up` writes **`.safeops/cluster-identity.json`**:

  ```json
  {"schema_version": 1, "cluster_name": "safeops",
   "control_plane_container": "safeops-control-plane", "container_id": "<full 64-hex ID>"}
  ```

  - The ID is the full immutable Docker container ID, never the short display form. It's read in the same `docker inspect` as the labels.
  - The write is atomic and durable: a 0600 temporary file in the same directory, fsync (`F_FULLFSYNC` on macOS), rename over, directory fsync.
  - Because it's written first, a later bootstrap failure (Helm, images, RBAC) still leaves the proof teardown needs.
- **No adoption.** `_preflight` refuses to start when a same-name cluster exists, before any kind call creates anything, so an identity is only ever recorded for the cluster `cluster-up` just made. There's no path that inspects an existing cluster and pins it.
- One authoritative implementation, `safeops/kind_identity.py` (stdlib only):
  - `verify_control_plane`: standard identity, the labels;
  - `record_identity`;
  - `verify_owned`: the labels plus the exact persisted ID.

### Teardown

The behavior of `down()`:

| Situation | Behavior |
|---|---|
| Cluster absent from `kind get clusters` | Idempotent success; a stale ownership record is removed once absence is established |
| Labels match and current full ID = persisted ID | Deleted |
| Labels match, ID differs | **Refused**: a same-name cluster replaced ours |
| Wrong cluster label, wrong role, unreadable labels or ID, container uninspectable | Refused |
| Record missing, malformed, incomplete, for another cluster or container, wrong or non-integer `schema_version`, unknown fields, short or uppercase ID | Refused; **never a fallback to name- or label-only deletion**. The record is left exactly as found |

- **Ordering is a hard invariant:** verify ownership → `kind delete` → confirm it succeeded *and* the cluster is no longer listed → only then remove `cluster-identity.json`.
  - A failed delete propagates and keeps the record, and the kubeconfig too, so the next attempt can still prove ownership.
  - A delete that "succeeds" while the cluster stays listed is an error, and the record is kept.
  - There is no path that erases ownership evidence before a confirmed delete.
- **The refusal message is actionable.** It gives:
  - the record's path, the container name, the expected and actual labels, the persisted and current IDs;
  - for a missing record, that it may be a legacy pre-pin cluster;
  - the one-time migration command;
  - a warning to verify independently that the cluster is safe to delete first;
  - the manual alternative `kind delete cluster --name safeops`, explicitly as one that bypasses SafeOps ownership verification.

### Kubernetes independence, and NodeInspector's stronger check

- **Teardown asks** "is this the exact kind cluster SafeOps created?". That must work when Kubernetes is broken, so Docker identity is sufficient and API reachability is not required.
- **NodeInspector asks** "is this the node behind the API the harness is using?". So it keeps the standard identity *plus* the published API-server endpoint (decision 0012). It doesn't need the container-ID pin: it never deletes anything.
- Teardown's independence is **proven offline, structurally**, not by breaking a live API server. With `Kubectl` and `NodeInspector` made to raise on use, kubectl and helm forbidden through the tool runner, and a garbage harness kubeconfig present, teardown still deletes using only `docker inspect` and `kind`. `kind_identity.py` imports only the standard library and has no kubectl, kubeconfig or API-endpoint code. The admin kubeconfig is passed to `kind delete` only so kind can drop its context entry; it is never ownership evidence.

### The operator override: argv only

- **The cost of a local pin:** if `.safeops/` is lost while the genuine cluster still exists, automatic teardown can't prove ownership. Quarantine must still fail closed, but a human needs a supported escape hatch.
- **The override is exactly** `uv run python -m safeops.bootstrap down --force-unverified-ownership`. No other spelling is accepted (a usage error, nothing runs), and it prints a warning.
- **What it bypasses:** only the container-ID match, so a missing or stale record. The kind labels are still required.
- **It is argv only, by design.** `_default_rebuild` runs `python -m safeops.bootstrap down` without replacing `env=`, so the child inherits the operator's environment. An environment-variable switch exported once for a migration would silently arm every later unattended quarantine. So:
  - no environment variable, configuration key or global state enables it;
  - tests set plausible variables (including `MAKEFLAGS`) and show that teardown still refuses and `down()` receives no override;
  - a structural test finds no `os.environ` or `getenv` in the identity module or in `down`/`main`, and no `force`/`owner` field in the cluster config;
  - `force_unverified_ownership=True` appears once, in `main()`.
- **It isn't wired into `make`,** because make variables can come from the environment.
- **Quarantine never uses it.**
  - `_default_rebuild`'s argv is fixed: `down`, then `up`.
  - An ownership refusal makes `cluster-down` exit non-zero. That surfaces as `QuarantineFailed` carrying the tail of the child's stderr (bounded to 1500 characters, and echoed through). So the identity details and migration guidance reach the operator, not an opaque "cluster-down failed".
  - The quarantine marker stays, the worker halts, and `up` never runs. There's no fallback.

### One-time migration of the pre-pin cluster

The cluster running when this landed was created before the pin, so it has no record:
- normal `make cluster-down`, and therefore any quarantine, refuses it, as designed;
- `cluster-up` will not adopt it.

The migration, done once and deliberately:

```bash
uv run python -m safeops.bootstrap down --force-unverified-ownership   # after confirming it's the SafeOps cluster
make cluster-up                                                       # fresh cluster, pinned at creation
```

No identity file is ever written by hand for the old cluster. From then on, normal teardown and rebuild verify the exact instance.

### Tests and mutation checks

`tests/test_cluster_ownership.py` (43 tests) covers:
- every row of the table above;
- the guidance;
- the argv-only override, including exact-spelling rejection, that it skips only the ID, the environment and structural checks, and that quarantine never uses it while surfacing the refusal;
- cluster-up pinning right after create, surviving a later failure, with mode 0600;
- no adoption;
- the atomic write;
- delete-failure and delete-without-effect keeping the record;
- the unlink ordering;
- Kubernetes independence.

The 0021 fakes now return the three-field inspect output.

**Mutation checks: 11, all killed:**
- no container-ID comparison;
- an unprovable record falling back to the labels;
- `cluster-up` adopting an existing cluster;
- teardown adopting a same-name cluster;
- the rebuild passing the override;
- an environment-variable bypass;
- the record removed before the delete;
- `cluster-up` never pinning;
- no post-delete confirmation;
- the override always on;
- the override skipping the labels.

## 3. The campaign image lock (specified now, carried to the freeze)

- **`.safeops/images.json` is a per-bootstrap observation.** It records what *this* bootstrap built, loaded and saw running. It's local state, rewritten by every `cluster-up`, including quarantine rebuilds, so it can't also be the frozen reference. A rebuild must never overwrite the only frozen image reference.
- **The campaign lock is a separate, version-controlled input under `config/`,** for example `config/campaign-images.json`, because `config/` holds the inputs whose changes alter the experimental condition. It's written once, at the freeze, and **never rewritten automatically** by bootstrap or rebuild.
  - **Contents:** exact immutable digests (never movable tags alone) for every image whose bits can change the experimental condition: the SafeOps testbed images, the database, the Prometheus and monitoring components, the Chaos Mesh components, and any other image already treated as experimental provenance. Not unrelated host images.
  - It carries a `schema_version`.
- **Lifecycle:** development → pilot(s) → pilot accepted → freeze the experimental configuration → write the campaign image lock under `config/` → the final campaign begins. Once frozen campaign mode is on, every bootstrap, quarantine rebuild, resumed worker and admitted run must match the lock.
- **Two distinct checks,** protecting different intervals; never collapsed into one:
  1. **Bootstrap/rebuild check.** Once the cluster is healthy enough to report its running image digests, compare them with the lock. A mismatch fails the bootstrap or rebuild, and the worker isn't admitted. This catches images changed by a rebuild.
  2. **Per-run admission check.** Before each admitted run, compare the running relevant images with the lock. A mismatch rejects admission, so the run isn't comparable final-campaign data. This catches drift after bootstrap.
- **A missing lock:**
  - before the freeze, it may be allowed, per the development and pilot workflow;
  - in frozen campaign mode, a missing, malformed or incomplete lock, or a digest mismatch, is an error;
  - the lock is never silently regenerated.
- **Scope and timing:**
  - Not implemented now: nothing before Milestone 6 needs it.
  - It's carried to the **Milestone 14 freeze**, where it must exist before the Phase A campaign starts.
  - This **supersedes** decision 0021 §4's carry-forward, which placed a drift check before the first pilot run (Milestone 10) against the bootstrap-time record. That check would have compared against a reference a rebuild rewrites. Pilot runs may proceed without a lock, and are flagged and excluded from the final results anyway (§15 Rule 22).

## Verification

- **Offline:** `make test` (940 passed), `make lint`, and `scripts/check_repo_policy.py` all pass. The design document is unchanged.
- **Mutation checks:** 17 in all (6 for the reset lifecycle, 11 for ownership), each killed and restored.
- **Live:** the cluster was migrated as above. Then `make cluster-test` ran against the freshly pinned cluster; its quarantine test rebuilds through the exact-ownership teardown. Finally `safeops verify` reported CLEAN.

From here on: implement a milestone, adversarially review it and its immediate seams, fix, verify, commit, move on. Older code is reopened only when later work exposes a concrete violated invariant.
