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
- **Projection version:** `snapshot_projection_version` is 2. Changing the semantic projection requires incrementing it, and `load_desired_state` refuses a file from another version.

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

## Amendment, 2026-11-19: Milestone 3 adversarial review

Each fix below has a regression test in `tests/test_m3_review.py`; H1, H2 and L5 also have live tests in `tests/cluster/test_lifecycle_live.py`.

**H1: nothing in the testbed is invisible.**
- **The finding:** a NetworkPolicy cutting off `orders`, a ResourceQuota, a bare pod and an extra Secret were all planted live, and verify said CLEAN every time. Reset didn't remove them, so they would have survived every reset: every later run would fail its steady state, and quarantine would never trigger.
- **The fix:** verify now also takes an **inventory** of every other kind the testbed could hold (`INVENTORY` in `manager.py`): pods, ReplicaSets, ControllerRevisions, ServiceAccounts, NetworkPolicies, ResourceQuotas, LimitRanges, HPAs, PDBs, Jobs, CronJobs, Ingresses, Roles and RoleBindings.
- **What's expected:**
  - pods owned, through their ReplicaSet, by a desired Deployment or a desired StatefulSet;
  - ReplicaSets and ControllerRevisions owned by desired workloads;
  - the `default` ServiceAccount;
  - SafeOps' own testbed RBAC objects, taken from the system manifests.

  Anything else is `unexpected object in the testbed`.
- **What reset deletes:** stray objects of the configuration kinds (NetworkPolicy, ResourceQuota, LimitRange, HPA, PDB, Job, CronJob, Ingress, extra ServiceAccounts).
- **What it leaves to quarantine:** stray pods, ReplicaSets, ControllerRevisions and RBAC objects. Verify flags them, which leads to quarantine, so the harness never needs pod-delete or RBAC-delete rights.
- **Residual:** Secrets are not inventoried, because no SafeOps identity reads Secrets. An extra Secret stays invisible. Nothing in SafeOps creates Secrets in the testbed except bootstrap, which creates `postgres-credentials`.

**H2: Pod Security baseline on `testbed` and `safeops-workload`.**
- **The finding:** the harness could create a Deployment whose pod was privileged, with `hostPID`, `hostNetwork` and the node's root filesystem mounted (proven by server dry-run). That's root on the node, which reaches the whole cluster.
- **The fix:** both namespaces now carry `pod-security.kubernetes.io/enforce: baseline`, and every testbed pod, Postgres included, meets it. Admission rejects privileged pods even for the admin credential, and the harness's Deployment path gets "would violate PodSecurity", so its pods are never admitted. Chaos Mesh's privileged daemon runs in `chaos-mesh`, which is unaffected.

**M1: verify fails closed on everything except guard and credential errors.** Before, only `CommandError` was caught. An odd quantity (`ValueError`) or a malformed object (`KeyError`) crashed verify instead of reporting dirty.

**M2: a failed pre-run reset is handled like a failed post-run reset.** A `CommandError` from the pre-run reset or verify becomes a dirty result: quarantine, then `PreRunDiscard`.

**M3: the record is always written.** A rebuild that times out or crashes becomes `QuarantineFailed`, with the marker kept. `_post_run_clean` catches *any* quarantine failure, so the run's record is finalized before the worker halts.

**L1:** a non-finite numeric value (`NaN`, `inf`) is kept as `non-finite:<raw>` text, so a state never differs from itself.

**L2:** a halted runner refuses to start another run.

**L3:** the node check also inspects namespaces pinned under `/var/run/netns`, matching decision 0012.

**L4:** `safeops quarantine-clear` lifts a quarantine after an operator fixes the cause, but only if verify is clean at that moment.

**L5:** reset now replaces (PUT) the Postgres StatefulSet on every reset, not only on projected drift, so unprojected drift can't persist. It's a no-op when nothing changed: same generation, and the pod isn't restarted (verified live). Its PVC is still never touched.

### Carried to Milestone 4 (M4): a separate settledness check for the post-fault baseline

The runner captures the pre-fault and post-fault snapshots with no stabilization window, but §11.1 says endpoint fields are captured only after stabilization. The post-fault snapshot is the harm baseline. This is resolved for Milestone 4 under "Milestone 4 requirement: post-fault settledness" in the third-review amendment below.

## Amendment, 2026-11-19: Milestone 3 second review

The regression tests are in the "second review" section of `tests/test_m3_review.py`, plus a live test for R5.

- **R1: configuration errors are not dirt.**
  - The first review's fail-closed fix also caught SafeOps' *own* misconfiguration: a stale or unreadable `desired_state.yaml`, a manifest render error, a topology error. It reported them as a dirty environment, so a pre-run verify triggered a quarantine rebuild that couldn't fix them.
  - `_configuration()` now loads everything reset and verify compare against *before* the fail-closed block, and raises `ConfigurationError` on failure. Configuration, guard and credential errors propagate. Only failures in reading or evaluating the *cluster* are reported as dirty.
- **R2: any post-run failure keeps the run.** Before, the post-run reset and verify caught only `CommandError`, so, for example, a harness token expiring mid-run lost the completed run's record. Any exception there is now a dirty result: the record is written, then quarantine runs. Quarantine's rebuild issues a fresh token, which also repairs that case.
- **R3: inert kinds are inventoried too.**
  - Added: DaemonSets, ReplicationControllers, PodTemplates and Leases, all deletable by reset.
  - An inert DaemonSet had been caught only indirectly, through its ControllerRevision. A zero-replica ReplicationController would have been invisible.
  - **Exact coverage claim:** the inventory covers the kinds listed in `INVENTORY`, plus the five projected kinds and Chaos Mesh objects. Not Secrets, by design, and not Events or the legacy `Endpoints` objects that controllers derive from Services.
- **R4: stale images are named, not discovered.**
  - Reset and verify now check that the images reset would deploy (the content tags from the current source) are the ones `cluster-up` loaded, according to `.safeops/images.json`. A mismatch raises `ConfigurationError("testbed source changed since cluster-up … run make cluster-down && make cluster-up")`.
  - Before this, a testbed source edit led to ImagePullBackOff, a 120 s timeout, a dirty verify and a full rebuild, with no message saying why.
  - Tests that use fakes pass `image_check=False`.
- **R5: hand-made EndpointSlices.**
  - An EndpointSlice injected into `orders-svc` was detected, but the stabilization check treated it as "not yet stable", so every verify waited the full window (125 s measured). Reset also couldn't remove it.
  - EndpointSlices are now inventoried. Expected slices are the ones managed by `endpointslice-controller.k8s.io` and owned by a desired Service; anything else is a stray that reset deletes (the harness gained `delete` on endpointslices).
  - Stabilization counts only controller-managed slices, so a stray is reported at once instead of stalling verify. The snapshot still counts *every* slice, because an injected endpoint is exactly the harm signal it exists to catch.

## Amendment, 2026-11-19: Milestone 3 third review

The regression tests are in the "third review" section of `tests/test_m3_review.py`.

**1. The record is finalized before any recovery.**
- **The problem:** the old order was `_post_run_clean` (which quarantined and rebuilt), then `finalize`, so destructive recovery sat between completed measurements and the durable record.
- **The order now** (`ExperimentRunner.run`):
  1. clean up: stop the workload, reset, verify;
  2. decide `reset_verified` (`_post_run_clean` is now pure);
  3. **finalize the record**;
  4. `_recover_if_dirty`: quarantine and rebuild if cleanup was dirty or failed;
  5. a failed recovery halts the worker (`QuarantineFailed`, kept in `_halt`), after the record exists.
- **Tests:** they drive a full run with a shared event log and assert `finalize` comes *before* `quarantine`, for each case: a dirty verify, a reset exception, a `stop_workload` exception, and a failed recovery. A mutation check put the old order back, and all four tests failed.
- `RUN_SEQUENCE` gains the stage "recover a dirty environment (after the record)".

**2. `stop_workload()` is post-measurement cleanup.**
- Effectiveness and harm are known before it runs, so an exception from it no longer escapes.
- It's recorded as a cleanup problem: `reset_verified` is False, the record is finalized, then recovery runs, since a generator that couldn't be stopped is rebuilt with the cluster.
- The completed measurements are never discarded.
- No run-record schema was invented for this; that's Milestone 5.

**3. A valid but stale `desired_state.yaml` is a configuration error.**
- `_configuration()` regenerates the desired snapshot from the current manifests and topology in memory, and compares it with the committed file, before any reset mutation or verification work.
- A difference raises `ConfigurationError("config/desired_state.yaml is stale; run make desired-state …")`. The file is never rewritten at runtime.
- **Test:** a committed state that is syntactically valid and at the right projection version, but differs from the manifests. Both reset and verify raise, and neither touches the cluster.

**4. The probe projection is complete.**
- **The audit:** the probe projection was checked once, against core/v1 `Probe` and `ProbeHandler`.
- **Now projected:** every field.
  - handlers: `exec.command`; `httpGet.{path, port, host, scheme, httpHeaders}`; `tcpSocket.{port, host}`; `grpc.{port, service}`;
  - timing: `initialDelaySeconds`, `periodSeconds`, `timeoutSeconds`, `successThreshold`, `failureThreshold`, and the probe-level `terminationGracePeriodSeconds`.
  - There are no intentional exclusions.
- **Header normalization:** names are lowercased, since they're case-insensitive in HTTP, and headers are sorted by (name, value). Values are kept exactly, and repeats are kept because a repeated header is meaningful.
- **`startupProbe` is projected too.** It isn't in §11.1's list, but it gates readiness and can restart the container.
- **Projection version:** the projection changed, so `snapshot_projection_version` is now **2**, and `desired_state.yaml` was regenerated.

### Milestone 4 requirement: post-fault settledness (carried from the third review; not implemented here)

- **Keep the existing stabilization check.** Milestone 3's health and readiness stabilization stays unchanged: reset and clean-state verification still require the environment to become *healthy*.
- **Add a separate, bounded settledness check** for the post-fault harm baseline. *Settled* is not the same as *healthy*: `bad-image-tag`, for example, is unhealthy but can be fully settled.
  - Before capturing the post-fault snapshot, poll the canonical projection until it is identical across two consecutive polling intervals, within a bounded timeout.
  - If it doesn't settle, record and flag the unsettled condition. Never silently treat an arbitrary mid-rollout snapshot as a clean harm baseline.
- **No per-fault declarations.** The property is observed from cluster state, never maintained as another list in the fault catalog.
