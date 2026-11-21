# SafeOps

[![ci](https://github.com/Tejas-Chakkarwar/SafeOps/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Tejas-Chakkarwar/SafeOps/actions/workflows/ci.yml)

**Risk-aware remediation for AI incident-response agents in Kubernetes.**

SafeOps is an incident-response system in which an AI agent investigates an alert with read-only tools, diagnoses the problem, and proposes a fix. Before that fix touches the cluster, a **safety gate** scores its operational risk and decides to execute it or escalate it to a human. Afterwards, two independent checks run: did the service recover (*effective*), and did the action cause new damage (*harmful*)?

The research question is how much autonomous remediation can be safely allowed at a given level of risk. The project answers it by comparing a risk-aware gate against simpler strategies (always act, always escalate, random, and a self-reported-confidence threshold) on resolution rate, harm, escalation rate, recovery time and cost, plotted as a risk-automation curve.

It's an MS Computer Science project at San José State University (CS297, Fall 2026 → CS298, Spring 2027), advised by Prof. Robert Chun.

> **Status:** under active development. Milestones 1–5 of 17 are complete: a reproducible testbed, a verified environment lifecycle, the first fault with symptom-only alerts, and a validated, immutable run record. The agent, the gate and the verifiers are still to come, so **there are no results yet**. See [Roadmap](#roadmap).

---

## How a run works

```
reset ─▶ verify clean ─▶ steady-state baseline ─▶ inject fault ─▶ verify symptom ─▶ settled baseline
                                                                                          │
   finalize record ◀── reset + verify ◀── effective? harmful? ◀── execute | escalate ◀── gate ◀── agent ◀── alert
```

Each run is one experimental unit, `(fault_id, fault_seed, trial_index)`, and writes exactly one immutable JSON record.

Design choices that keep the numbers honest:

- **An information wall.** The agent, feature extractor and gate never see the fault's identity, the seed or the remediation lists. The alert is built only from the fault's *observable symptom*, after the harness has measured it holding. An import test enforces the boundary.
- **Harm is measured against the post-fault snapshot,** not the pre-fault one, so the agent isn't blamed for the fault's own damage. Snapshots are canonical semantic projections, never raw object diffs.
- **Two safety layers.** A hard envelope (RBAC and an executor whitelist) makes some actions impossible. Within it, the gate judges what's *wise*, so harm stays measurable rather than hidden by the envelope.
- **Agent mistakes are results, not failures.** Only infrastructure and provider failures, failed resets, and silent non-injection are discarded, and even discards keep their record.
- **A fixed denominator.** Baselines and recovery are measured over fixed windows of scheduled requests on a load generator the system under test can't reach. Prometheus is never used for these measurements.

The full specification is [`docs/safeops-design.md`](docs/safeops-design.md) (Revision 5.1, frozen through the pilot). Every implementation decision since, with its reasoning, is in [`docs/decisions/`](docs/decisions/).

## Testbed

A local [kind](https://kind.sigs.k8s.io/) cluster with four services and a database:

```
load generator ─▶ gateway ─▶ orders ─┬─▶ inventory
                                     ├─▶ payment
                                     └─▶ postgres (StatefulSet)
```

The cluster also runs Prometheus, for the agent's telemetry, and [Chaos Mesh](https://chaos-mesh.org/), for network and resource faults. The service images are built reproducibly and verified by digest.

**Security model:** three identities.
- **`experiment-controller`** is the harness. It is namespace-scoped, uses a time-limited token, and never falls back to the admin credential.
- **`safeops-reader`** is read-only (Milestone 6).
- **`safeops-executor`** may patch only whitelisted resources, never StatefulSets (Milestone 8).

Every `kubectl` and `helm` call passes an explicit context, and a guard refuses anything outside the phase's scope.

## Quick start

**Prerequisites** (macOS, tested on Apple Silicon):
- Docker Desktop with about 8 GB of memory;
- `kind`, `kubectl`, `helm`;
- [`uv`](https://docs.astral.sh/uv/), which provides Python 3.12 from `.python-version`.

**Offline**, with no cluster needed:

```bash
make sync    # sync the local environment; dependency changes may refresh uv.lock
make test    # offline test suite
make lint    # ruff check + ruff format --check
```

**Live:**

```bash
make cluster-up      # create the kind cluster, build images, deploy the testbed
safeops steady-state # measure one 300-request baseline window
safeops verify       # check the testbed against config/desired_state.yaml
safeops reset        # reset to desired state, then verify
make cluster-test    # live RBAC, lifecycle, fault and run-record tests
make cluster-down
```

`safeops run --fault bad-deployment-config --seed 0 --trial 0` runs every stage that exists so far, from the reset through fault injection to the symptom-only alert. It stops at the agent, which arrives at Milestone 9.

## Repository layout

| Path | Contents |
|---|---|
| `safeops/` | The system: CLI, environment lifecycle, snapshots, faults, run records, and later the agent, gate, executor and verifiers |
| `testbed/` | The testbed services and load generator (Python), built into reproducible images |
| `manifests/`, `kind/` | Kubernetes manifests, Helm values, the kind cluster definition |
| `config/` | Topology, desired state, workload and cluster configuration; changing these changes the experimental condition |
| `faults/` | Fault specifications (YAML) |
| `docs/` | The design document and the decision log |
| `scripts/` | Repository-policy tooling, not part of the experiment |
| `tests/` | Offline tests; `tests/cluster/` holds the live-cluster tests |

## Quality bar

- **CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) checks two things on every push.
  - It reproduces the offline checks from a clean checkout against the committed lockfile.
  - It enforces repository policy, all fail-closed:
    - the design document is frozen;
    - no generated data, local state or credentials are tracked;
    - the decision log is numbered without gaps.
- **Live tests run deliberately at milestone boundaries**, never in CI.
- **Hand-written, tested code** for the parts whose bugs produce wrong numbers without crashing: the canonicalizer, the run-record validator, and later the harm checker and the metrics. Safety- and experiment-critical rules are covered by negative tests and mutation checks.

## Roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Scaffold, CLI, loud stubs | ✅ |
| 2 | Testbed, topology, workload generator | ✅ |
| 3 | Environment lifecycle and canonical snapshots | ✅ |
| 4 | First fault and symptom-only alert | ✅ |
| 5 | Run record schema and writer | ✅ |
| 6 | Read-only tools and `safeops-reader` RBAC | next |
| 7 | Effectiveness verifier and harm checker | |
| 8 | Parser, feature extractor, gate, executor (with fabricated proposals) | |
| 9 | Model adapter and agent loop | |
| 10–17 | Pilot, fault catalog, gate weights, replay, campaign, analysis, Helm packaging, demo | |

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE).
