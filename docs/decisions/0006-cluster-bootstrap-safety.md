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

## Amendment, 2026-11-19 (Milestone 2): open items resolved

**1. Kubeconfig.** The admin kubeconfig lives at `kubeconfig_path: .safeops/kubeconfig`, gitignored along with everything under `.safeops/`. `kind create cluster --kubeconfig` writes there, so `~/.kube/config` is never touched and `minikube` stays the current context. Every command passes **both** `--kubeconfig` and `--context`. Runtime uses a second credential, `.safeops/experiment-controller.kubeconfig`, and never the admin one (decision 0009). The guard picks the credential by phase, so the phases are separated by credential, not only by guard logic.

**2. Bootstrap scope, extended by amendment.** The bootstrap namespaces are now `testbed`, `chaos_namespace` (`chaos-mesh`), `system_namespace` (`safeops-system`, decision 0009), `workload_namespace` (`safeops-workload`, decision 0009) and `monitoring_namespace` (`safeops-monitoring`, decision 0010). Runtime still allows `testbed` only.

The cluster-scoped set is **recorded mechanically** in `config/bootstrap_scope.yaml`:
- `make bootstrap-scope` renders both pinned charts with their install values (`helm template`, CRDs included) plus the SafeOps manifests, and writes every cluster-scoped `(kind, name)`.
- `make cluster-up` re-renders before creating anything and **refuses on any difference**.
- After creating the cluster, it also checks that every recorded kind really is cluster-scoped according to `kubectl api-resources --namespaced=false`.
- Every namespaced object a chart renders must target its release namespace, or bootstrap refuses.
- Chart and manifest objects are applied only after each one passes `check_object` in the bootstrap phase.

Rendered on 2026-11-19:

| Source | Cluster-scoped objects |
|---|---|
| Chaos Mesh 2.8.8 | 24 CRDs (`*.chaos-mesh.org`); 6 ClusterRoles and 6 ClusterRoleBindings (controller-manager, dns-server, and dashboard, each `-cluster-level` and `-target-namespace`); MutatingWebhookConfiguration `chaos-mesh-mutation`; ValidatingWebhookConfigurations `chaos-mesh-validation` and `chaos-mesh-validation-auth` |
| Prometheus chart 29.47.2 | ClusterRole and ClusterRoleBinding `prometheus-server` |
| SafeOps manifests | Namespaces `testbed`, `safeops-system`, `safeops-workload`, `safeops-monitoring`, `chaos-mesh` |

- **Dashboard RBAC:** the chart renders the dashboard's ClusterRoles even with `dashboard.create=false`. The procedure records what the chart actually produces, not what was expected.
- **No `cluster-admin`:** nothing in the set grants it, and `tests/test_cluster_guard.py` asserts that.

**3. Guard tests.** `tests/test_cluster_guard.py` covers:
- runtime refuses every namespace other than `testbed` and anything cluster-scoped;
- bootstrap allows exactly its namespaces and its recorded cluster scope;
- an AST check confirms that only `safeops/bootstrap.py` (and the definition in `safeops/cluster.py`) references `Phase.BOOTSTRAP`;
- every kubectl call carries `--kubeconfig` and `--context`, and never reads `current-context`;
- `Kubectl.read` refuses mutating verbs;
- `apply` checks every object before running anything;
- runtime raw calls can only reach the workload-generator proxy.

**Workload proxy.** The workload-generator proxy (decision 0009) is not a Kubernetes-object mutation, so `check_object` doesn't govern it. `WorkloadProxy` has its own capability check instead (namespace, Service, operation ∈ {stats, control}).
