# 0003 — Fault backend: Chaos Mesh works on kind under Docker Desktop (macOS)

**Status:** Accepted (week-one spike, 2026-11-18)
**Design refs:** §11.2 (Fault Injector, `FaultBackend`), §16 risk "Chaos Mesh fragile on macOS", Appendix A
**Decision:** Keep Chaos Mesh as the primary backend for network-latency and resource-pressure faults. Toxiproxy and Litmus stay as unused fallbacks for now.

## Context

§11.2 warns that Chaos Mesh's components are Linux-oriented and may need workarounds under Docker Desktop on macOS, and asks for a week-one spike. This was an explicitly authorized spike outside the numbered milestones. It does not implement any part of Milestone 4.

## Environment

| Item | Value |
|---|---|
| Host | macOS (Darwin 24.6.0), Apple Silicon (arm64) |
| Docker Desktop | Engine 28.3.2, 10 CPUs, ~7.65 GiB, cgroup v2, kernel 6.10.14-linuxkit |
| kind | v0.33.0, single node, Kubernetes v1.37.0, containerd 2.3.4 |
| Helm | v4.3.2 |
| Chaos Mesh | chart and app **2.8.8** (latest on `charts.chaos-mesh.org` at spike time), from the official Helm repo. The chart in `reference/chaos-mesh/` was **not** used (we don't run reference installers). |

Isolation: a throwaway cluster `chaos-spike` with its **own kubeconfig file** in a scratch directory. Every command used `--context kind-chaos-spike`. The default kubeconfig and its `minikube` context were never touched. The cluster was deleted afterwards.

## Install

```bash
helm install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --version 2.8.8 \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --set dashboard.create=false --wait
```

Ready in about 14 s. That's 3 controller-manager replicas, 1 chaos-daemon and the chaos-dns-server, with 23 CRDs. The containerd runtime and socket settings are the only kind-specific settings needed. Nothing else needed a macOS workaround.

## Test workload

Namespace `spike`: an `nginx:1.27-alpine` server Deployment with a Service, and a `curlimages/curl` client Deployment. No resource limits.

## Results

**NetworkChaos `delay`** (200 ms, jitter 0, `mode: all`, selector `app=server`):

| Phase | Client→server `curl` time_total (n=10) |
|---|---|
| Baseline | median 0.7 ms |
| Injected | median ~405 ms (403–409 ms) |
| After deleting the CR | median 0.7–0.9 ms |

- `tc qdisc` in the server's network namespace showed `netem delay 200ms` while the fault was active and was back to `noqueue` afterwards.
- One request takes about 2× the configured delay, because the delay applies to the target's **egress**: both the SYN-ACK and the HTTP response are delayed.

**StressChaos `cpu`** (2 workers, load 100, selector `app=server`):

| Phase | CPU used by the server container's cgroup (5 s window) |
|---|---|
| Baseline | 0.00 cores |
| Injected | 2.00 cores; `stress-ng --cpu 2 --cpu-load 100` visible on the node |
| After deleting the CR | 0.00 cores; no `stress-ng` processes left |

**Repeatability:** 3 more inject→clear cycles of each fault gave the same numbers every time. No leftover qdisc or stress process, the CRs were gone, and pods were never restarted (0 restarts across the spike).

## Consequences

1. **Chaos Mesh stays primary** for network-latency and resource-pressure faults (§11.2 table unchanged). There's no need to switch to the Toxiproxy fallback now.
2. **Clearing a fault = deleting the Chaos CR.** That matches `FaultBackend.remove(spec)`. Reset must still verify that the effect is gone, not just the CR: no netem qdisc and no stress process. §11.1's "never trust, verify" applies.
3. **Latency faults compound per round trip.** At Milestone 4/11, size a `network-latency` fault and its `symptoms:` expression by measured end-to-end latency, not the configured per-packet delay.
4. **CPU stress alone produced no latency symptom here.** The target had no CPU limit and the node had 10 cores, so nginx was never starved. A resource-pressure fault needs CPU limits on the target (the testbed sets them per `topology.yaml`), or `verify_fault_present` will correctly report silent non-injection.
5. **Chaos Mesh CRs are harness resources.** They are created by `experiment-controller` and excluded from snapshots (§11.1 "Explicitly ignored"). `safeops-reader` and `safeops-executor` get no RBAC on `chaos-mesh.org`.

## Not covered by this spike

- Multi-node kind clusters, and `PodChaos` / `IOChaos` / `HTTPChaos` / DNS faults. Pod kill uses the Kubernetes API anyway (§11.2).
- Long-running stability across hundreds of inject/clear cycles. Milestone 11's "5 seeds per fault" check will show that.
- Running alongside the full testbed and Prometheus within Docker Desktop's ~7.65 GiB memory limit. Check at Milestone 2.

## Follow-ups (from review, 2026-11-18)

None of these block Milestone 1. Each is tagged with the milestone where it lands. Consequences 3 and 5 above stand as written. Nothing here changes the design document, so this file is the only record.

### F1 — Pin the Chaos Mesh version · Milestone 2 (config + install), Milestone 5 (provenance)

The spike used 2.8.8 only because it was the latest release. Without a pin, a later `helm install` could pull a different version and change fault behavior mid-campaign.

- Add `chaos_mesh_version: 2.8.8` to `config/cluster.yaml` (the same file that holds the `kind-safeops` context and `testbed` namespace, decision 0002).
- `make cluster-up` passes `--version` from that file. The version is never hardcoded in the Makefile or scripts.
- Record the fault backend in run record provenance, next to `kind_version` and `kubernetes_version`, as a **backend-generic** object rather than a Chaos-Mesh-specific field:

  ```json
  "fault_backend": {"name": "chaos-mesh", "version": "2.8.8"}
  ```

  `FaultBackend` is swappable (§11.2), so a Toxiproxy-backed scenario records `{"name": "toxiproxy", "version": "..."}` with no further schema change. For faults that go through the Kubernetes API directly (pod kill, config faults), use `{"name": "kubernetes-api", "version": null}` unless Milestone 5 decides otherwise. `chaos_mesh_version` stays in `config/cluster.yaml`, because that file controls the actual cluster install. The provenance field records which backend a run used and which version.
- **This is a schema addition to §11.8.** When the record schema is built at Milestone 5, log it as its own decision. Don't edit the design doc.

### F2 — One controller-manager replica · Milestone 2

Three controller-manager replicas on a single-node kind cluster add nothing. Memory is tight: Docker Desktop's ~7.65 GiB has to fit Chaos Mesh, four services, Postgres, Prometheus and the workload generator. Install with:

```bash
--set controllerManager.replicaCount=1
```

Memory is the most likely thing to bite at Milestone 2, and this headroom costs nothing.

### F3 — Clean-state verification must check node-level state · Milestone 3 (extends acceptance criterion)

**Invariant:** clean-state verification must independently confirm that no fault-backend side effect relevant to the implemented fault types remains after cleanup. A missing Chaos CR is not enough evidence.

Initial checks, one per fault type the spike exercised:

| Chaos kind | Residual effect `verify_clean_state` must rule out |
|---|---|
| `NetworkChaos` | no residual `netem` qdisc in any testbed pod's network namespace |
| `StressChaos` | no residual `stress-ng` workload on any node |

Each new backend-driven fault type added later (Milestone 4, Milestone 11) adds its row here or in its own decision.

This is a different kind of check from comparing canonical resources against `desired_state.yaml`, because it looks below the Kubernetes API. A qdisc left behind after a chaos-daemon crash is exactly the silent corruption that would make every later result wrong.

**Extended Milestone 3 acceptance criterion** (recorded here, since the design doc is frozen): in addition to §14's criterion, a residual netem qdisc and a residual stress-ng workload, each planted deliberately with no Chaos CR present, must each make `verify_clean_state` fail.

**Mechanism is open until Milestone 3.** Options include host-side `docker exec` into the kind node containers, a privileged node-debug pod, or a small helper running alongside the chaos daemon. Record the chosen identity accurately:
- **Host-side `docker exec`** from the Mac does **not** use the `experiment-controller` ServiceAccount. It is a host-side harness privilege outside Kubernetes RBAC.
- **An in-cluster pod** would run under `experiment-controller` (or a dedicated harness identity), and needs privileges beyond the §4 diagram, so record them.

Either way the privilege belongs to the harness, which sits outside the SafeOps trust boundary (§4), and `safeops-reader` and `safeops-executor` never get it.

### F4 — `topology.yaml` resource ranges must be real container limits · Milestone 2

CPU stress produced no symptom because the target had no limits. The `cpu_millis` and `memory_mib` ranges declared per deployment in `topology.yaml` must be applied as **actual container requests and limits in the testbed manifests**, not only carried as metadata for range checks. Otherwise `memory-limit-low` and the resource-pressure faults will inject cleanly, produce nothing, and `verify_fault_present` will correctly reject every run.

Milestone 2 check: every testbed Deployment's manifest sets CPU and memory requests and limits, each value inside that deployment's `safe_range`.
