# 0012 — Node-level clean-state checks: host-side `docker exec` into the kind node

**Status:** Accepted (2026-11-19), Milestone 3 preflight. Resolves the open mechanism in decision 0003, F3.
**Design refs:** §4 (identities), §11.1 (`verify_clean_state`, quarantine), decision 0003 (F3), decision 0006 (cluster identity)

## The requirement (0003, F3)

After reset, `verify_clean_state` must **independently** confirm that no fault-backend side effect remains. The absence of a Chaos CR is not evidence. The initial checks are:

| Residue | Evidence of it |
|---|---|
| Network chaos (`NetworkChaos`) | a `netem` or `tbf` qdisc in any network namespace on the node |
| Stress chaos (`StressChaos`) | a `stress-ng` or `memStress` process on the node (by process name or command line) |

## Options considered

| Mechanism | Independent evidence? | Widens Kubernetes privileges? | Size |
|---|---|---|---|
| **Host-side `docker exec` into the kind node container** | yes: it reads the node's kernel state directly | **no** | smallest: no new Kubernetes object |
| Privileged debug or helper pod | yes | **yes**: a privileged pod with host PID and host network, plus RBAC to create it, in some namespace | medium |
| Dedicated node-level daemon or helper | yes | **yes**: a privileged DaemonSet running for the whole cluster lifetime | largest |

## Decision: host-side `docker exec`

It's the smallest mechanism that gives independent evidence, and it grants no Kubernetes privilege at all.

### Trust-boundary semantics, stated exactly

- It is a **host-side experiment-harness capability**: the harness process uses the local Docker socket on the machine running kind.
- It operates **outside Kubernetes RBAC**. Kubernetes doesn't mediate it, and no ServiceAccount, Role or binding grants it.
- It is **not** performed by the `experiment-controller` ServiceAccount. That identity's RBAC (decision 0009) is unchanged by this decision. Host Docker access is a separate capability that the harness process happens to have, and it must not be described as anything granted through RBAC.
- It is used **only** for the independent clean-state invariant. The commands are fixed, read-only inspection scripts (list qdiscs, list processes) that take no caller input. It's never used to change node state; reset and quarantine don't use it.
  - The only exception is outside SafeOps code: test code (`tests/cluster/`) plants residue deliberately in order to prove the check fails.
- SafeOps identities (`safeops-reader`, `safeops-executor`) never get it. The agent never reaches it.

### Scoped to the SafeOps kind node, and fails closed

Before any inspection, the harness checks that the target container really is the SafeOps kind node. Any mismatch, missing container, or Docker error refuses the check, and **the clean-state result is then "not clean"**: a check that can't run never counts as passing. The identity checks are:

1. A container named `<cluster_name>-control-plane` exists (from `config/cluster.yaml`).
2. Its label `io.x-k8s.kind.cluster` equals `cluster_name`, and its label `io.x-k8s.kind.role` is `control-plane`.
3. The loopback port Docker publishes for its `6443/tcp` is the API server port in the harness kubeconfig, the same identity rule as decision 0006 C3.

So the node being inspected is provably the node behind the cluster the harness talks to.

### What it inspects

- **Every network namespace on the node:**
  - the root namespace;
  - every CNI pod namespace under `/var/run/netns/`;
  - every distinct `/proc/<pid>/ns/net`, so a namespace that isn't registered under `/var/run/netns` is still covered.

  In each one, `tc qdisc show` must list no `netem` or `tbf` qdisc.
- **The node's process table:** no process whose name (`comm`) or command line is `stress-ng` or `memStress`.

**Extension points.** New backend-driven fault types (Milestone 4, Milestone 11) add their residue checks here; for example, iptables or ipset rules if partition chaos is ever used. Each one needs its own planted-residue test.

## Acceptance (Milestone 3)

A planted `netem` qdisc in a testbed pod's network namespace, and a planted process named `stress-ng`, each with **no Chaos CR present**, must each make `verify_clean_state` fail. A real `NetworkChaos`, once its CR is deleted by reset, must leave no residue, so `verify_clean_state` passes again.

## Amendment, 2026-11-19 (Milestone 3 review, L3)

The first implementation walked only `/proc/*/ns/net`, so a namespace pinned under `/var/run/netns` with no process left in it was skipped, despite this decision saying otherwise. The script now inspects both sources, deduplicated by namespace inode, as specified above.
