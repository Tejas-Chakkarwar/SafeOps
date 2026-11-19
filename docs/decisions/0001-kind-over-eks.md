# 0001 — Local kind only; EKS is a post-core stretch goal

**Status:** Accepted (2026-11-18)
**Design refs:** §2 (scope, stretch goals), §4 (no cloud credentials)

## Decision

The core project runs on a local `kind` cluster under Docker Desktop on macOS, and nowhere else. SafeOps never receives cloud provider credentials. The EKS portability demo (Terraform plus the same Helm chart) is a stretch goal. It becomes active only when the author explicitly activates it, after the core project is complete.

## Reasons

- **Reproducibility:** the experimental condition is fully local and version-pinned (kind, Kubernetes, the testbed, Chaos Mesh), and recorded in run provenance.
- **Cost and safety:** Phase A deliberately executes harmful actions (§8). That should happen only on a disposable local cluster that can be rebuilt in minutes, never against cloud resources or billing.
- **Scope:** cloud portability adds nothing to the evaluation question (§1).

## Consequences

- There are no cloud SDKs, cloud credentials, or Terraform in the core project.
- Other kube contexts exist on the development machine (currently `minikube`). The context guard (decision 0002) is what enforces "kind only". Intent alone does not.
