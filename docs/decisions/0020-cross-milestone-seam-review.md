# 0020 — Cross-milestone seam review (Milestones 1–5)

**Status:** Accepted (2026-11-20), between Milestones 5 and 6. Milestone 5 stays closed; this is a separate review.
**Design refs:** §6 (the information wall), §11.1 (canonical snapshots), §15 Rules 1, 6, 7; decisions 0004, 0013, 0018

## Why this review exists

Earlier reviews mostly validated each milestone on its own. This pass looked at invariants shared *across* milestone boundaries: places that were correct when written, but can become unsafe or inconsistent once a later milestone builds on them.

None of the four findings invalidates a collected result, since the campaign hasn't started. But three would become dangerous as Milestones 6 and 7 add consumers.

## 1. The information wall: a namespace boundary over evaluator and harness code

- **The defect:** the wall test protected `safeops.agent`, `safeops.features` and `safeops.gate`, but forbade only `safeops.faults`. Ground truth isn't confined there. For example, `safeops/experiment/unit.py` holds `RunUnit` (`fault_id`, `fault_seed`, `trial_index`), and a protected module could have imported it unnoticed. Nothing does today, so it was latent, not a live leak.
- **The prerequisite:** forbidding `safeops.experiment` outright would have collided with the next milestones. `FeatureVector`, `Proposal` and `TrajectoryStep` lived in `safeops/experiment/schema.py`, and those are exactly what the protected packages must use: the gate takes a `FeatureVector` (Rule 7), the parser produces a `Proposal`, and the agent produces `TrajectoryStep`s.
- **Chosen: Option A, narrowly.** The genuinely neutral types moved to a new module, **`safeops/domain.py`**:
  - `StrictModel`, the strict base;
  - `Sha256`, `UnitInterval`, `NonNegInt`;
  - `TrajectoryStep`, `Proposal`, `RecommendedAction`, `ActionTarget`, `FeatureVector`;
  - `ShadowGates`, which the protected `safeops.gate.shadow` produces.

  The run-record schema imports and reuses these **same classes**, with no duplication; a test checks `FeatureVector` is one class. Run-record versioning, strict record validation, the record-only models (`Gate`, `Execution`, `DiscardReason`, …) and the cross-field rules stay in `safeops.experiment`. No rule changed, and all 810 existing tests passed unchanged after the move.
  - **Not `safeops/models/`:** that package is for LLM model adapters (§11.4, Milestone 9).
  - **Why not B or C:** an allowlisted module (B) is a permanent hole that could quietly gather unsafe contents. A list of forbidden modules (C) needs every future ground-truth module added by hand. A namespace boundary needs neither.
- **The boundary**, enforced by `tests/test_information_wall.py`. These are forbidden, transitively, through relative imports, and inside function bodies:

  | Namespace | Why |
  |---|---|
  | `safeops.faults` | fault ground truth |
  | `safeops.experiment` | the experimental unit, runner, records, schema |
  | `safeops.verify` | verdicts |
  | `safeops.environment` | the harness credential and the evaluator's measurement path |
  | `safeops.bootstrap` | the admin credential |
  | `safeops.analysis` | analysis over records |

  - **Protected code consumes only neutral modules:** `safeops.context` (`IncidentRuntimeContext`), `safeops.domain`, `safeops.enums` and `safeops.topology`. These are themselves in the checked set, so they must stay neutral too.
  - **New tests:**
    - every forbidden root must exist (the mirror of "the protected packages exist"), so a rename can't make the wall pass vacuously;
    - the modules that hold ground truth today are named, so a move is noticed;
    - each forbidden root is caught, and lazy and relative imports are caught too;
    - the neutral types are reachable without a leak.
  - **Mutation check:**
    - un-forbidding `safeops.experiment` fails 4 tests;
    - a real protected module importing `RunUnit` fails a test;
    - `safeops.domain` reaching the harness fails 2 tests;
    - a lazy import of the harm checker in the real agent fails a test.
- The protected packages' docstrings now name this boundary, instead of saying only "never import `safeops.faults`".

## 2. An empty `diff()` and an equal `digest()` mean the same identity

- **The defect:** `_diff_values` compared dict values through `a.get(k)` and `b.get(k)`. So a key present with `None` and a missing key were "identical" (an empty diff) while their digests differed.
  - **The same class of bug, found while fixing it:** leaves compared with Python `==`, so `1`, `1.0` and `True` were equal although their JSON, and so the digest, differs.
  - Clean-state verification and the stale-desired-state check consume `diff()`. Milestone 7's harm comparison will too, and its headline number must not depend on a comparator whose notion of equality differs from the canonical hash.
- **The fix, in the generic comparator:**
  - key membership is explicit: a key only on the expected side is `missing`, only on the actual side is `unexpected`, and the comparator recurses only into keys on both sides;
  - leaves compare as canonical JSON, the form the digest hashes;
  - list semantics are unchanged.
- **Also:** `diff()` refuses to compare snapshots of different projection versions.
- **The invariant:** `diff(a, b) == []` exactly when `a.digest() == b.digest()`.
  - It's tested in both directions over the real desired-state projection (Deployments, Services, ConfigMaps, the StatefulSet, the PVC; nested dicts, lists, null fields; resources appearing and disappearing), perturbed 60 ways with a fixed seed.
  - There's an explicit regression for None-versus-missing, because real projections have fixed shapes that may never produce it.
  - Restoring either old comparison fails tests.
- Settledness keeps its extra digest comparison, now documented as belt and braces, not compensation.

## 3. One `PROJECTED_KINDS`

- **The defect:** it was defined as an identical literal in both `safeops/topology.py` and `safeops/environment/snapshot.py`, and different subsystems imported different copies. If the projection gained a kind and the topology's copy were forgotten, ownership validation and clean-state verification could silently stop seeing objects of that kind.
- **Owner: `safeops/topology.py`.** It's the lower layer (stdlib-only, imports nothing internal), so the snapshot module imports it with no cycle and re-exports it. A neutral module wasn't needed.
- **Test:** the two names are the same object, and an AST scan of `safeops/` finds exactly one assignment. Reintroducing a second literal fails it.

## 4. Persisted snapshots carry their projection version

- **The defect:** `Snapshot.to_json()` didn't serialize the version, so `Snapshot.from_json()` stamped whatever the current `SNAPSHOT_PROJECTION_VERSION` was onto old data. After a future projection bump, an old snapshot would have claimed to belong to the new projection.
- **Callers, checked before any change:**
  - `digest()` hashes `to_json()`, and feeds the run-record snapshot hashes and `desired_state_hash`;
  - `desired.py` renders `config/desired_state.yaml` from `to_json()`, with its own version field;
  - `safeops snapshot` printed `to_json()`;
  - `from_json()` had no production caller, only a round-trip test.
- **The fix keeps hashing and persistence separate:**
  - `to_json()`, and `digest()` over it, is **unchanged**. The desired-state snapshot's digest is byte-identical under the old and new code (`sha256:d436d879…`), so no recorded or provenance hash changes meaning, and a golden digest test pins the canonical form.
  - `to_persisted_json()` writes `{projection_version, resources}`.
  - `from_persisted_json()` refuses another version and refuses an unversioned snapshot. A reader never silently stamps the current version onto old data.
  - `from_json()` is removed.
  - `safeops snapshot` now prints the persisted, versioned form, and its digest still goes to stderr.
- **Tests:** a current-version round trip; the version surviving it; versions 1, 3, `"2"`, `true` and null refused; the unversioned canonical form refused. Removing the version check or the shape check fails tests.

## Scope

No Kubernetes behavior changed. `make test` and `make lint` pass, and so does the repository-policy checker. No cluster run was needed: these are import boundaries, pure comparison logic, the ownership of a shared constant, serialization, and documentation.

**From here on, earlier milestones are reopened only when a later milestone exposes a concrete regression, not by another broad review.**
