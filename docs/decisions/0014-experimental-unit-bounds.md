# 0014 — The experimental unit: seed and trial bounds, fault-id validation, seed → fault

**Status:** Accepted (2026-11-19), Milestone 4. Resolves two open items from decision 0004 ("Notes for later milestones"): the seed range, and validating the fault id before it is used as a path.
**Design refs:** §10.1 (the unit), §11.8 (`unit` in the run record), §9.5 (fault spec)

Resolved now, before seeds drive fault construction, not deferred to Milestone 5.

## Bounds

| Field | Range | Why |
|---|---|---|
| `fault_seed` | `0 … 2^32 − 1` | See below |
| `trial_index` | `0 … 999` | See below |

**`fault_seed` fits an unsigned 32-bit integer.**
- **JSON:** 2^32 − 1 is far below 2^53, so a seed is stored exactly in a run record by every JSON reader, including JavaScript's.
- **RNGs:** the only RNG that consumes the seed today is CPython's `random.Random`, which takes integers of any size. A 32-bit bound also fits every seeded RNG we might plausibly use later, unchanged and without hashing or truncation: NumPy's legacy `RandomState` refuses anything ≥ 2^32, and many C and Go generators take 32- or 64-bit seeds. Truncating a seed silently would make two "different" seeds inject the same fault.
- **Room:** 4.29 billion seeds. Milestone 11 uses 5 per fault.

**`trial_index` is at most 999.** It is `0 … k−1` (§10.1), and `k` comes from pilot variance (§18). It drives no RNG (`random-p` is seeded from `run_id`, Rule 24), so JSON is the only hard limit. The tighter bound exists to catch mistakes, such as a seed passed as `--trial`. Raise it if the pilot ever needs `k > 1000`.

**Where they're enforced:** `safeops/experiment/unit.py` (`RunUnit.__post_init__`, `MAX_FAULT_SEED`, `MAX_TRIAL_INDEX`). The CLI's `--seed` and `--trial` take the same constants (out of range is exit 2), and `FaultSpec.variant_for` refuses an out-of-range seed again. Booleans are refused even though Python treats them as integers. The bounds belong in the Milestone 5 record schema too.

## Fault id: validated before it is a path

- The pattern is `[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?`, ASCII only, matched with `fullmatch`. That means 1–64 lowercase letters, digits and inner hyphens, with no dots, slashes, whitespace or trailing newline.
- It is checked in three places: `RunUnit` (CLI and runner); `fault_path()` before `faults/<id>.yaml` is built; and against the file's own `id:` field.
- `fault_path()` also resolves the path and requires it to sit directly inside `faults/`, so a symlink planted in `faults/` that points elsewhere is refused.
- A `--fault` that doesn't name an existing spec is a usage error (exit 2) before any cluster contact.

## Seed → injected fault

- A fault spec lists `injection.variants`. The seed selects exactly one, with `index = int(random.Random(seed).random() * n)`.
- That is the one output CPython documents as stable across versions for the same seed. `randrange` and `choice` carry no such guarantee. Python is pinned at 3.12 anyway (decision 0005).
- `tests/test_m4_faults.py` pins the mapping for seeds 0–11 and 2^32 − 1 as golden values. If it ever changes, every seed's injected fault has changed, and the test says so.
- For `bad-deployment-config`, seeds 0, 1, 5 and 7 select variants 3, 0, 2 and 1, so all four are covered.
- Every variant of a fault must produce the same symptom at a comparable magnitude (decision 0016). The seed varies *how* the fault is realized, not *whether* it is detectable.
