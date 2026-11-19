# 0013 — Environment lifecycle: reset, clean-state verification, snapshots, quarantine

**Status:** Accepted (2026-11-19), Milestone 3
**Design refs:** §5 (three state captures, stabilization window), §11.1 (lifecycle, snapshots, reset-failure handling), §15 Rules 6, 15, 16, 23; decisions 0006, 0011, 0012

## Desired state

- **`config/desired_state.yaml`** is the canonical snapshot that a clean testbed must match.
  - It's **generated** from `manifests/testbed` and `config/topology.yaml` by `make desired-state`, never hand-written, and committed. Its hash belongs in run provenance (Milestone 5).
  - Expected Service endpoint counts are derived from the selected workloads' replicas, and expected owners from the topology.
  - `tests/test_snapshot.py` regenerates it and fails on any difference. So changing a manifest, the topology, or testbed code (image tags are content-derived) without regenerating it can't go unnoticed.

## Canonical snapshot (`safeops/environment/snapshot.py`, hand-written per Rule 16)

- **Projection:** exactly the §11.1 fields, with resource requests and limits kept separate (decision 0011).
  - **Normalized:** API-server defaults (probe timings, `scheme: HTTP`, port `protocol: TCP`, `type: ClusterIP`) and equivalent quantities (`0.1` = `100m`, `268435456` = `256Mi`).
  - **Dropped:** status (except PVC bound), resourceVersion, generation, managedFields, timestamps, UIDs, all annotations (including `kubectl.kubernetes.io/restartedAt` and the revision annotation), `clusterIP` values (only "headless or not" is projected), and Pods, ReplicaSets and Events as objects.
  - **ConfigMaps:** a ConfigMap in the topology projects the sha256 of its approved keys plus every numeric approved key's parsed value. A ConfigMap that isn't in the topology is hashed in full, so a rogue one is visible.
- **Service endpoints:**
  - `ready_endpoint_count` counts EndpointSlice endpoints with `ready: true` (not null).
  - `endpoint_owner_services` resolves each endpoint's pod through **ownerReferences** (Pod → ReplicaSet → Deployment, or Pod → StatefulSet), then through the topology. It never uses labels, which a rogue pod could copy. So `orders-svc` repointed at inventory pods reports owner `inventory`, even though the endpoints look healthy.
- **Scope:** every projected-kind object in `testbed` is included except `kube-root-ca.crt`, so an unexpected object appears as `unexpected` and a removed one as `missing`.
- **Projection version:** `snapshot_projection_version` is 1. Changing the projection bumps it, and `load_desired_state` refuses a file from another version.

## Reset (`EnvironmentManager.reset_environment`, runtime phase)

1. Delete every Chaos Mesh object in `testbed`, and wait until it's gone, so Chaos Mesh recovers its effects.
2. Delete every projected-kind object that the desired state doesn't contain. That covers rogue objects and stray PVCs; it keeps `kube-root-ca.crt` and the StatefulSet's expected PVC.
3. **Recreate the app tier** (Deployments, Services, ConfigMaps) from the desired manifests: delete with foreground cascade and wait, then create ConfigMaps, then Services, then Deployments.
   - **Why recreate instead of patch:**
     - Revision history restarts at 1 on every run, so `get_rollout_history` and `rollback_deployment` see the same history in every run. They never see an earlier run's fault revisions, which would leak information between runs and make the agent's view nondeterministic.
     - Recreating also drops every field another field manager added, which server-side apply would keep.
   - Verified live: after a reset every Deployment is at revision 1.
4. **Postgres:** the StatefulSet is replaced only if its projection drifted. Its PVC and data are never touched.
   - **Row data persists across runs.** No fault in the catalog and no SafeOps action touches row data, and the table grows by about 10 rows per second only while load runs. Revisit if a data-touching fault is ever added.
5. Wait for the stabilization window.

**Reset never inspects or changes node state.** Node residue is found by verify and handled by quarantine (Rule 23: no hand-patching).

## Clean-state verification (`verify_clean_state`)

It runs independently of reset: it doesn't trust that reset succeeded. It's clean only if **all** of these hold:

- the stabilization window closed without timing out;
- `diff(desired_state, live_snapshot)` is empty;
- no Chaos Mesh object remains in `testbed`;
- the node-level checks find no residue (decision 0012).

**It fails closed:** a cluster read that fails (`CommandError`) or a node check that can't run is reported as a problem, never as clean. Guard and credential errors propagate instead, because they're misconfiguration, not dirt.

## Stabilization window

Every Deployment must meet all of these:
- `observedGeneration` ≥ `generation`;
- `replicas`, `updatedReplicas`, `readyReplicas` and `availableReplicas` all equal `spec.replicas`, so no surplus old pods remain.

Every StatefulSet must have all replicas ready and `currentRevision` equal to `updateRevision`. No pod may still be terminating. Every Service's ready endpoint count must equal its selected ready pods, so endpoints have caught up. The timeout is 120 s (§5). A timeout returns False; the run is flagged (Milestone 5), not discarded.

## Quarantine and reset failure (§11.1)

- `quarantine_and_rebuild(reason)` writes `.safeops/quarantine.json` **first**, then recreates the cluster through the real `python -m safeops.bootstrap down` and `up` entry points.
  - So it never widens the guard: bootstrap is still reachable only through those entry points (decision 0006). It's never a hand-patch.
  - Then it verifies clean state. The marker is removed only when the rebuilt environment verifies clean. Otherwise `QuarantineFailed` is raised and the marker stays. While a marker exists, `reset_environment` refuses (`WorkerQuarantined`): the worker is halted.
- **Pre-run dirty** (runner hook `_require_pre_run_clean`): quarantine and rebuild, then raise `PreRunDiscard`. The run is discarded and counted.
- **Post-run dirty** (`_post_run_clean`): the run is **kept** with `reset_verified = false`, then quarantine and rebuild.
  - A reset that raises a `CommandError` counts as a failed reset, not a lost run.
  - If the rebuild itself fails, the run's record is still finalized first, and then the worker halts.
  - Completed runs are never discarded for a failed cleanup: the hardest runs to clean up are the ones where the agent did the most damage.

## The harness identity's testbed rights

Role `experiment-controller-testbed-lifecycle`, in `testbed`, scoped to what reset, verify and snapshots need. It extends decision 0009, and its testbed-scope list is narrower than §4's "full control":

| Resource | Verbs |
|---|---|
| Services, ConfigMaps, PVCs, Deployments, StatefulSets | get, list, watch, create, update, patch, delete |
| Pods, ReplicaSets, EndpointSlices | get, list, watch |
| `chaos-mesh.org` (all) | get, list, watch, delete. Creating chaos is Milestone 4. |

There's no Secret access, no `pods/exec`, no pod delete, and nothing outside `testbed`. Bootstrap's `kubectl auth can-i` checks and `tests/test_testbed_manifests.py` pin this.

## Testbed fix found at Milestone 3

The first live reset took 135 s. The testbed processes run as PID 1, and PID 1 ignores SIGTERM by default, so every pod deletion waited out the full 30 s grace period. `testbed/app/common.py` now exits promptly on SIGTERM. This changed both image digests, as expected, since the base stage changed.

## CLI

- `safeops snapshot` prints the canonical JSON (its digest goes to stderr).
- `safeops verify` checks the current state.
- `safeops reset [--times N]` runs reset, then verify, N times.
- Exit codes: 0 means clean; **6** means not clean; 5 means a guard, credential or cluster error.
