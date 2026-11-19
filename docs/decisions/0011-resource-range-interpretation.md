# 0011 — How `cpu_millis` and `memory_mib` ranges apply to requests and limits

**Status:** Accepted (2026-11-19), at the Milestone 2 closeout, before the Milestone 3 snapshotter exists
**Design refs:** §8 (hard vs safe ranges), §9.2 (`patch_deployment_resources`), §11.1 (projected fields), §11.7 (state violations), §12 (`topology.yaml`)

## Why decide now

`config/topology.yaml` gives each Deployment one `cpu_millis` range and one `memory_mib` range, each with a `hard_range` and a `safe_range`, but a container has both a request and a limit. Milestone 3's canonical snapshotter must not inherit an ambiguous schema.

## Decision

- **Both values are checked independently.** The `cpu_millis` and `memory_mib` ranges apply separately to the **request** and to the **limit**, wherever those fields are evaluated: the executor's hard envelope (Milestone 8), the harm checker's safe-range checks (Milestone 7), and the manifest tests (Milestone 2; `tests/test_testbed_manifests.py` already checks each one).
- **Snapshots keep them separate.** Canonical snapshots (Milestone 3) preserve the request and the limit as separate values, per container, and never collapse them into one value such as a max, a sum, or "the limit only".
- **Comparison is deferred.** Milestone 7 decides the harm-comparison mechanics on top of that: how the severity rule (§11.7) combines a request violation and a limit violation in one container.

## Note for Milestone 4: `memory-limit-low` can't lower the limit alone

- **The constraint:** Kubernetes rejects a pod spec in which `limits.memory` < `requests.memory`. The testbed requests 256 Mi (the safe floor), so `memory-limit-low` can't be injected by lowering only the limit. The API server refuses it.
- **For the fault (Milestone 4/11):** it must lower the request and the limit together.
- **For the remediation:** `patch_deployment_resources` must be able to raise both back.

This isn't solved here. It's recorded so that Milestone 4 doesn't discover it as a confusing API rejection during injection.
