# 0002 — Cluster identity: `kind-safeops` / `testbed`

**Status:** Accepted (2026-11-18). Amended by 0006: the `testbed`-only rule below is the **runtime** guard. Cluster bootstrap (`cluster-up`, `cluster-down`) has its own separately scoped guard.
**Design refs:** §4, §11.1; CLAUDE.md "Context guard"

## Decision

| Item | Value |
|---|---|
| kind cluster name | `safeops` |
| kube context | `kind-safeops` (kind always prefixes contexts with `kind-`) |
| testbed namespace | `testbed` |

These values live only in `config/cluster.yaml`. The context guard and every cluster command read them from there. No code, script or Makefile hardcodes them.

## Context guard

Before any mutating Kubernetes command, verify that:

1. the context in use is `kube_context` from `config/cluster.yaml`, and
2. the target namespace is `namespace` from `config/cluster.yaml`.

If either is not true, stop and report. Never fall back to "the current context". Pass the context explicitly (`--context`) rather than relying on `kubectl config current-context`.

## Why it matters now

The only context on the development machine today is `minikube`, and it is the current one. An unguarded `kubectl delete` would hit minikube.

## Implementation timing

Milestone 1 has no cluster commands, so it creates `config/cluster.yaml` but no guard code. The guard is implemented at Milestone 2, together with the first mutating command (`make cluster-up`). It must read `config/cluster.yaml` and be tested against a wrong context and a wrong namespace.
