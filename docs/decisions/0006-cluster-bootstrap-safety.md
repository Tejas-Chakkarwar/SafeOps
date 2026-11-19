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

## Amendment, 2026-11-19: Milestone 2 adversarial review fixes

Every fix below has a regression test in `tests/test_m2_review.py`, labelled with its finding ID. Before the fix, each test reproduced the problem.

**The wrapper can no longer be pointed elsewhere (G1–G4).**
- `Kubectl.read` refuses any caller argument that would override the explicit identity: `--context`, `--kubeconfig`, `--cluster`, `--user`, `--server` / `-s`, `--token`, `--as*`, client certificate and TLS flags. Both `--flag value` and `--flag=value` forms are refused.
- The reason is that kubectl uses the *last* occurrence of a flag. That was confirmed live: a later `--context` silently won over ours.
- `read` also refuses `--raw`, so raw paths only go through `Kubectl.raw` and its runtime path check.
- `read` allows only read-only subcommands: `auth can-i` and `rollout status`. `auth reconcile` writes RBAC.

**Scope comes from the kind, never from the object (G5/G6).**
- `Kubectl.apply` classifies each object as cluster-scoped from the built-in kind list plus whatever the live API server reports (`api-resources --namespaced=false`, asked once per `Kubectl`).
- The caller no longer supplies that list.
- A cluster-scoped kind that carries `metadata.namespace: testbed` is treated as cluster-scoped, so runtime refuses it. Kubernetes ignores that field for such kinds, so trusting it would have let runtime write cluster-wide.

**Bootstrap scope is classified against the live API (B1).**
- `render-scope` runs offline, so it classifies with the built-in list plus the scope declared by CRDs in the same render.
- `cluster-up` then re-classifies every rendered object against the live API's cluster-scoped and namespaced kinds, and refuses:
  - if the result differs from the recorded scope;
  - if a kind is unknown to the API server and not defined by a CRD in the render.
- So a cluster-scoped kind missing from the built-in list can no longer slip through unrecorded.

**Charts are pulled once and pinned by content (B3).**
- `helm pull` downloads each chart exactly once per `cluster-up`.
- `helm template` and `helm install` both use that same archive.
- Its sha256 is recorded in `config/bootstrap_scope.yaml` as `chart_sha256`:
  - chaos-mesh 2.8.8: `sha256:35f3cade…`
  - prometheus 29.47.2: `sha256:ed5e5fc1…`
- A republished chart under the same version now refuses.

**Credential files are private from their first byte.** The harness kubeconfig is created with `O_EXCL` and mode 0600, instead of being written and then chmod'ed. The `.safeops/` directory is 0700.

**The bootstrap-phase check catches every spelling (T1).** The AST check now also catches `Phase("bootstrap")`, `Phase["BOOTSTRAP"]`, and any other `Phase(...)` call outside `safeops/cluster.py` and `safeops/bootstrap.py`. A self-test proves the detector can fail.

## Amendment, 2026-11-19: second review (credential routing, cluster identity)

The regression tests are in the "second review" section of `tests/test_m2_review.py`.

- **C1: runtime refuses the admin credential however it's passed.** `Kubectl(cfg, Phase.RUNTIME, kubeconfig=...)` raises `CredentialError` if the path is the admin kubeconfig. Before this, the rule held only when no kubeconfig was passed. An AST test also confines `kubeconfig_path`, the admin path, to `safeops/cluster.py` and `safeops/bootstrap.py`.
- **C3: the context must really be the kind cluster.** The context name alone proves nothing, because a stale or copied kubeconfig could call anything `kind-safeops`. Before its first command, every `Kubectl`, for both phases and both credentials:
  - resolves the API server the `kube_context` context points at;
  - requires it to be a loopback address that Docker publishes for `<cluster_name>-control-plane` (`docker port … 6443/tcp`).
  - Anything else refuses, and nothing runs. Tests can opt out with `verify_identity=False`, but only together with a fake runner.
