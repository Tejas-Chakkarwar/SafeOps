# 0006 — Cluster bootstrap safety: a phase-aware context guard

**Status:** Accepted (2026-11-18). Amends decision 0002.
**Design refs:** §4 (identities), §8 (hard envelope), §11.1; CLAUDE.md "Context guard"

## Problem

Decision 0002 restricts mutating commands to the `testbed` namespace. That rule describes **runtime** operations, and as written it forbids Milestone 2's own work:

- `kind create cluster` and `kind delete cluster` aren't namespaced at all.
- Chaos Mesh installs into its own namespace (`chaos_namespace`, currently `chaos-mesh`) and creates cluster-scoped resources such as CRDs.

## Decision: the guard is phase-aware, not a flat namespace allowlist

| Phase | How it's reached | What it permits |
|---|---|---|
| `runtime` (**default**) | every per-run path: fault injection, reset, snapshots, the executor, verification, and anything else that runs per run | the `namespace` from `config/cluster.yaml` (`testbed`) only |
| `bootstrap` (**explicit opt-in**) | only from `cluster-up` and `cluster-down` | the SafeOps kind cluster itself (`cluster_name`), the `testbed` namespace, the `chaos_namespace`, and only the cluster-scoped resources required by the pinned Chaos Mesh chart's install path |

- `guard(phase="runtime")` is the default. A caller that doesn't name a phase gets runtime.
- `guard(phase="bootstrap")` must be requested by name, and only the cluster-up and cluster-down entry points may request it. It is **not** generic cluster-admin. Anything outside the scope above is refused even during bootstrap.
- **Why a phase model rather than a wider allowlist:** bootstrap runs a handful of times in the project's life, and runtime runs thousands of times. Widening the namespace allowlist to cover bootstrap would widen every runtime path too. So the narrow scope is the default, and the wide one is hard to reach by accident.

### What "cluster-scoped resources required by the pinned chart" means

The allowed set isn't written by hand from memory. At Milestone 2, render the pinned chart (`helm template` at `chaos_mesh_version`, with the install values from decision 0003: containerd runtime and socket, dashboard off, `controllerManager.replicaCount=1`). Record the exact cluster-scoped kinds and names it produces (CRDs, ClusterRoles, ClusterRoleBindings, webhook configurations, or whatever it actually renders) in this file or an amendment to it. The bootstrap guard permits exactly that set. A chart version bump re-renders and re-records it.

## Explicit context on every command

- Every `kubectl` and `helm` invocation passes `--context` (Helm: `--kube-context`) explicitly, with the value taken from `kube_context` in `config/cluster.yaml`.
- `kubectl config current-context` is **never read and never relied on.** No SafeOps or harness code path depends on which context is current.
- So `minikube` can stay the current context, and that is the safer arrangement: a stray `kubectl delete` typed in another terminal must not land on the SafeOps cluster.

## Open items for Milestone 2 (not decided here)

1. **Where kind writes its kubeconfig.** `kind create cluster` sets the current context in the kubeconfig it writes to. If that's the default `~/.kube/config`, `minikube` stops being current, which works against the arrangement above. The options are a separate kubeconfig file passed with `--kubeconfig`, as the Chaos Mesh spike did (decision 0003), or restoring the previous current context after creation. Decide at Milestone 2 and record it here.
2. **The monitoring stack's bootstrap scope.** The scope above covers Chaos Mesh only, because that's the only backend decided. Once the Prometheus install is decided at Milestone 2, its namespace and cluster-scoped resources must be added to the bootstrap scope by amending this decision. Don't quietly widen the guard.
3. **Guard tests (Milestone 2):**
   - runtime refuses the wrong context, a namespace other than `testbed` (including `chaos_namespace`), and anything cluster-scoped;
   - bootstrap refuses the wrong context and anything outside its recorded scope;
   - only the cluster-up and cluster-down entry points can obtain bootstrap.
