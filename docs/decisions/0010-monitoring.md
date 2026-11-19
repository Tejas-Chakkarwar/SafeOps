# 0010 — Monitoring: a stripped Prometheus server with explicit scrape jobs

**Status:** Accepted (2026-11-19), Milestone 2
**Design refs:** §3 (Prometheus in the cluster), §11.3 (`get_metrics`), §11.5 (`telemetry_conflict`, `evidence_sufficiency`), Appendix A (`helm-charts`)

## Decision

- **Chart:** `prometheus-community/prometheus` **29.47.2** (Prometheus v3.16.0), pinned as `prometheus_chart_version` in `config/cluster.yaml`. It's installed with `--repo`, so the Helm repo list isn't touched.
- **Namespace:** `safeops-monitoring` (`monitoring_namespace`).
- **Server only:** Alertmanager, pushgateway, node-exporter and kube-state-metrics are off, the config-reload sidecar is off, and there's no Grafana (this chart doesn't include it).
- **Same architecture, lighter install.** The design names `kube-prometheus-stack` only as a reference repo. The architecture needs a Prometheus server, so this is a lighter deployment of the same thing, not a change to the experiment.
- **Values:** `manifests/helm/prometheus-values.yaml`.

| Setting | Value | Reason |
|---|---|---|
| `scrape_interval` | 5 s | Symptom expressions such as "for 2m", fault verification and effectiveness all work on windows of tens of seconds. At 5 s, `get_metrics`' cap of 60 points (§11.3) covers 5 minutes. |
| `scrape_timeout` | 4 s | Must be shorter than the interval |
| `retention` | 2 h | A run takes minutes. Telemetry older than a run is never needed, and short retention bounds memory on a single node with about 7.65–10 GiB. |
| storage | emptyDir (no PV) | Nothing needs to survive a Prometheus restart beyond the current run |
| resources | requests 256 Mi / 100m, limits 1 Gi / 1000m | Leaves room on one kind node |

## Scrape jobs are part of the experimental condition

Scrape configuration defines what the agent can observe. "We used the chart defaults" isn't a defensible description of an agent's sensory input, so every job is listed explicitly, and every chart default that isn't needed is disabled by name.

| Job | Enabled | What it gives the agent |
|---|---|---|
| `prometheus` | yes | Prometheus self-scrape (scrape health) |
| `safeops-testbed-pods` | yes | application metrics of pods in `testbed` annotated `prometheus.io/scrape: "true"` (the four services; Postgres isn't annotated) |
| `safeops-workload-generator` | yes | `loadgen_*` metrics of the generator in `safeops-workload`. Agent-visible only; the effectiveness verdict never reads them (0009) |
| `safeops-cadvisor` | yes | container CPU and memory telemetry, and memory-pressure signals, for `testbed` containers only (`metric_relabel_configs` keeps `namespace="testbed"`) |
| `kubernetes-api-servers`, `kubernetes-nodes`, `kubernetes-nodes-cadvisor` (replaced by `safeops-cadvisor`), `kubernetes-service-endpoints`, `kubernetes-service-endpoints-slow`, `prometheus-pushgateway`, `kubernetes-services`, `kubernetes-pods`, `kubernetes-pods-slow` | **no** | — |

**cAdvisor's role is limited.** It provides container CPU and memory telemetry plus memory-pressure signals. It isn't the authoritative source for OOMKilled state, because container metrics can disappear after a restart. Durable evidence for `memory-limit-low` comes from pod status and events, through `get_pods` and `get_events`. That split gives the agent two types of telemetry that can corroborate each other, which is what `evidence_sufficiency` and `telemetry_conflict` reason about.

**Verification:** the rendered chart was checked with `helm template` on 2026-11-19 and has exactly these four jobs. `cluster-up` then waits until every expected target is up: 1 Prometheus, 8 testbed pods, 1 generator, 1 node.

## Bootstrap scope

The chart's cluster-scoped objects (ClusterRole and ClusterRoleBinding `prometheus-server`, needed for service discovery and node cAdvisor access) are recorded in `config/bootstrap_scope.yaml`, per the procedure in 0006.
