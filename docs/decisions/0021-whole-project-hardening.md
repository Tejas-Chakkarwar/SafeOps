# 0021 — Whole-project hardening before Milestone 6

**Status:** Accepted (2026-11-20), between Milestones 5 and 6. Milestone 5 stays closed and Milestone 6 is current; this is a separate hardening pass, not milestone work.
**Design refs:** §6 (the information wall), §11.2 (clean-state verification), §11.8 (provenance), §15 Rules 1, 10; decisions 0002, 0006, 0012, 0013, 0018, 0020

Decision 0020 closed the seams between milestones. This pass closes twelve remaining gaps: places where a check could pass without having looked, could act on the wrong object, or documented less than it guarantees. After it, **earlier milestones are reopened only when a later milestone exposes a concrete regression** (0020's closing rule, restated).

## 1. Qdisc inspection fails closed

- **The defect:** the node script ran `tc qdisc show 2>/dev/null` in each network namespace and printed whatever came back. When `nsenter` was refused, `tc` was missing, or every `tc` call failed, the output was empty, and empty output parsed as "no netem, no tbf": **clean**. A check that couldn't look reported that it had looked.
- **The fix,** in `safeops/environment/node.py`:
  - tc's errors are no longer discarded;
  - a namespace that still exists but can't be inspected prints the exact marker `SAFEOPS_QDISC_INSPECTION_ERROR:` with the namespace and the first error line; so does a `tc` that succeeds but prints nothing (every namespace has at least a loopback qdisc);
  - `parse_qdisc_output` raises `NodeCheckError` on any marker line, and on output with no qdisc lines at all. `NodeCheckError` already makes verification fail closed (decision 0012).
  - Only the exact marker counts. tc output is never searched for words like "error": an interface named `error0` isn't a failure.
  - netem and tbf detection are unchanged.
- **A refinement over a literal "any failure is an error":** discovery races aren't failures.
  - The script lists `/proc/*/ns/net`, and processes exit while it runs. Treating each vanished pid as an inspection error would make verification flaky on a healthy node, and a flaky check gets disabled.
  - So pids are grouped per namespace and tried **oldest first**; the first that can be entered answers for the namespace.
  - If every pid of a namespace has exited, the namespace is gone. A destroyed namespace holds no qdisc, so it can't hold residue. It prints `SAFEOPS_QDISC_NAMESPACE_GONE:`, which the parser skips.
  - If any of its pids is still alive and none could be entered, that's the error marker.
  - Gone lines don't count as inspected qdiscs: output that is *only* gone lines still raises.
  - Pinned namespaces under `/var/run/netns` are handled the same way.
- **Live:** on the clean node the script printed 53 qdisc lines across 18 namespaces, with no error or gone markers and no residue.
- **Tests** (`tests/test_hardening.py`): clean → `[]`; netem and tbf → residue; empty and whitespace-only output → `NodeCheckError`; only gone lines → `NodeCheckError`; a gone line beside real ones → fine; either marker form → `NodeCheckError`; `error0` isn't the marker; the shell markers equal the Python constants; `NodeInspector.qdisc_residue` raises on empty exec output. Live: the clean node is silent and inspected at least 6 namespaces; a planted tbf (no Chaos CR) fails verify, alongside the existing planted-netem test.
- **Mutation checks:** removing the empty-output rejection fails 5 tests; ignoring the marker fails 2; counting gone lines as qdiscs fails 1.

## 2. Legacy core/v1 Endpoints are inventoried

- **The gap:** clean-state verification inventoried EndpointSlices but not core/v1 `Endpoints`. A hand-made Endpoints object would have survived reset unseen.
- **Observed live on the pinned cluster** (Kubernetes v1.37.0):
  - `endpoints` is still served, with full verbs; kubectl warns "v1 Endpoints is deprecated in v1.33+".
  - There is exactly one per Service (`gateway-svc`, `inventory-svc`, `orders-svc`, `payment-svc`, `postgres-svc`), named like it, each labelled `endpoints.kubernetes.io/managed-by: endpoint-controller` (plus the Service's `app` label; postgres also `service.kubernetes.io/headless`), with the annotation `endpoints.kubernetes.io/last-change-trigger-time`, no ownerReferences, and one subset.
  - Every testbed Service has a selector.
- **Classification** (`safeops/environment/manager.py`): an Endpoints object is expected only in that precise controller-managed form: `managed-by: endpoint-controller`, **no** ownerReferences, and named like a desired Service. Anything else is unexpected, so dirty, and deletable by reset:
  - a rogue name, even with the controller's label;
  - a desired name without the label, or with another manager;
  - a desired name with an ownerReference (the controller sets none, so that shape isn't its object).
- **RBAC:** the testbed lifecycle Role gains `endpoints: get, list, watch, delete`, so the harness can see and remove them. **Not** `create`, `update` or `patch`; the manifest test asserts both directions, and the live RBAC checks gained the same three rows.
- **The canonical harm projection is not changed.**
  - kube-proxy routes from EndpointSlices, not Endpoints.
  - EndpointSliceMirroring mirrors Endpoints into slices only for a Service *without* a selector, and every testbed Service has one.
  - So a rogue Endpoints is an inventory problem, not a state change the projection should see. The live test confirms it: a rogue Endpoints produces no mirrored EndpointSlice, leaves the snapshot digest unchanged, is reported by verify, and is deleted by reset.
  - If a later milestone adds a selector-less Service, this reasoning no longer holds and the projection must be revisited.
- **Mutation checks:** dropping the controller-form check fails 3 tests; dropping the name check fails 1; allowing ownerReferences fails 1; removing Endpoints from the inventory fails 5.

## 3. `cluster-down` proves identity by kind labels

- **The defect:** `cluster-down`, which quarantine rebuilds call unattended, deleted whatever `kind get clusters` listed under the configured name. A same-name cluster belonging to something else would have been deleted.
- **One shared primitive,** `safeops/kind_identity.py` (stdlib only): `verify_control_plane(cluster_name, runner)` inspects the container `<cluster>-control-plane` and requires the Docker labels `io.x-k8s.kind.cluster = <cluster>` and `io.x-k8s.kind.role = control-plane`. Its error names the container, the expected values and what it found.
- **Two callers, deliberately different:**
  - `NodeInspector.verify_identity` (decision 0012) uses it, then **also** requires the node to publish the harness kubeconfig's API server. Inspection must prove it's looking at the cluster the harness talks to.
  - `bootstrap.down` uses it **alone**. Teardown must not need API-server reachability: a quarantine rebuild runs exactly when the cluster may be broken, and must not stall on it.
  - The old label check inside NodeInspector is gone; a test asserts no second copy of the label names exists in `bootstrap`.
- **Behavior:**
  - cluster absent from `kind get clusters` → idempotent: nothing is inspected or deleted, local files are cleaned up;
  - labels match → deleted, even with the API server unreachable;
  - wrong cluster label, wrong role, missing labels, garbled output, or a container that can't be inspected → refused with `CommandError`, nothing deleted.
- **Tests:** offline only, with injected kind and Docker fakes and the config's local paths redirected to a temporary directory. No identity test is destructive against a real cluster. The real teardown of the SafeOps cluster, which has the correct labels, ran during this pass's rebuild and in the quarantine live test.
- **Mutation checks:** skipping the identity check in teardown fails 5 tests; ignoring the role fails 2; ignoring an inspect failure fails 1.

## 4. What `image_digests` and `running_images` mean

- Both are **bootstrap-time, frozen provenance**: the digests built and loaded at `cluster-up`, and the images the testbed ran then. They don't prove what a run executed against.
- Documented in `ProvenanceSource` (`safeops/experiment/records.py`) and as an amendment to decision 0018.
- **A live drift check before admitted runs** (running images re-read and compared with the recorded digests) belongs to campaign admission, not here. It is **required before the first pilot run (Milestone 10)** and recorded there as a carried requirement. Not implemented now.

## 5–6. The information wall protects `safeops.tools`

- `safeops.tools` is added to the protected set in `tests/test_information_wall.py`. Milestone 6 puts the agent's read-only tools there; tool code reading fault ground truth would be as bad as the agent reading it.
- **Negative fixtures:** a tool importing `safeops.environment.workload`, `safeops.experiment.unit`, `safeops.faults.spec` or `safeops.verify.harm` is each caught. A test asserts `safeops.tools` stays protected. Removing it from the set fails a test.
- **Wording.** The wall is a **forbidden-boundary invariant**, not an exhaustive allowlist: it names namespaces protected code must never reach. The neutral modules (`safeops.context`, `safeops.domain`, `safeops.enums`, `safeops.topology`) are an illustrative list of what's safe today, not a closed set. The test docstring and the protected packages' docstrings now say this.

## 7. Documentation

- CLAUDE.md: the current direct runtime dependencies are PyYAML, Typer and Pydantic, with `pyproject.toml` as the source of truth.
- README: `make sync    # sync the local environment; dependency changes may refresh uv.lock`.

## 8. The rebuild drops cached clients: recorded, not changed

- After a quarantine rebuild, `EnvironmentManager` drops its cached `Kubectl` and `NodeInspector`, because the rebuild issued new credentials and a new API endpoint. Clients a caller injected are never dropped.
- This is correct and stays. A test pins it: defaults are dropped and rebuilt lazily, injected doubles survive. Removing either drop fails the test.

## 9–12. This record, tests, verification, commit

- Every code change above has negative tests and a mutation check.
- Verification: `make test`, `make lint` and `scripts/check_repo_policy.py` pass. The Role change needed a cluster rebuild (`make cluster-down && make cluster-up`, which exercised the identity-checked teardown on the real cluster), then `make cluster-test`.
- No Kubernetes behavior visible to an experimental run changed except the inventory: one more kind is checked and cleaned. The canonical projection, `digest()`, and every record field are unchanged.
- Committed separately from milestone work.
