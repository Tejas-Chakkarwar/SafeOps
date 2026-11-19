# 0007 — Testbed: services, configuration split, images, topology values

**Status:** Accepted (2026-11-19), Milestone 2
**Design refs:** §9.2, §9.5, §9.6, §11.8 (`image_digests`), §12

## Services

Four services plus Postgres, exactly as §12 describes: `gateway` → `orders` → {`inventory`, `payment`, `postgres`}.

- The workload calls `GET /checkout` on `gateway`. `gateway` calls `POST /orders` on `orders`. `orders` reserves stock with `inventory`, charges `payment`, then inserts a row into Postgres. Every request exercises the whole graph.
- **`/checkout` is an operation, not a service.** It's the gateway's user-facing operation, while `orders` is a service identity. The Revision 5.1 problem was a nonexistent *checkout service* in the topology, which became `orders`, not the word "checkout". Keeping `/checkout` as the route is a decision, not a leftover from that fix.
- Every service exposes `/healthz` (liveness), `/readyz` (readiness) and `/metrics`, and logs JSON lines filtered by `LOG_LEVEL`. `orders` reports ready only after its schema exists.
- `http_requests_total{service,route,code}` and `http_request_duration_seconds` are the agent-facing metrics. `orders` also exposes pool gauges (`db_pool_max_size`, `db_pool_size`, `db_pool_available`, `db_pool_requests_waiting`) and `db_pool_timeouts_total`.
- **The language is Python 3.12** (stdlib HTTP server, `prometheus-client`, `psycopg`), the same language as the rest of the project.

## Configuration split

Which object holds a value decides which remediation can repair it. Get this backwards and a fault becomes unrepairable by its own declared remediation.

| Must be repairable by | Lives in | Consumed how | Examples |
|---|---|---|---|
| `rollback_deployment` | the pod template (plain `env` values, probes, resources, image) | rolled out with the template, so a rollback restores it | `INVENTORY_URL`, `PAYMENT_URL`, `UPSTREAM_TIMEOUT_MS`, `ORDERS_URL`, `CACHE_MB` |
| `patch_configmap` | a ConfigMap whose keys are listed as `approved_keys` in `topology.yaml` | `env.valueFrom.configMapKeyRef`, read once at container start, so the composite's patch plus rollout-restart picks it up | `db-pool-config.max_pool_size`, `db-pool-config.connection_timeout_ms`, `orders-config.log_level` |

- **`bad-deployment-config`** (Milestone 4) changes a pod-template env var, which creates a new revision that a rollback genuinely repairs. It never modifies a ConfigMap: a rollback restores the pod template from a prior ReplicaSet and doesn't touch an independently modified ConfigMap.
- **`max_pool_size` is a real knob from the start.** It maps to `psycopg_pool.ConnectionPool(max_size=...)`, and `connection_timeout_ms` is the pool's checkout timeout. Plain psycopg has no pool, so `psycopg[pool]` is used (installed as `psycopg[binary,pool]`; `binary` bundles libpq). `db-pool-exhaustion` itself is Milestone 11.
- **Tests:** `tests/test_testbed_manifests.py` checks that every approved ConfigMap key is consumed by its owner through `configMapKeyRef`, and that the rollback-repairable values are plain pod-template env.

## Remediation lists and the Milestone 11 test

See decision 0008. Milestone 11 must execute at least one representative valid remediation for every fault that declares one, and confirm the service recovers.

## Images

- **Two images from one multi-stage `testbed/Dockerfile`:** `safeops-testbed` (the system under test; all four Deployments) and `safeops-loadgen` (the workload generator).
  - They share only the `base` stage: the Python base image pinned by digest, `prometheus-client`, and `app/common.py`.
  - Measurement code (`app/window.py`, `app/loadgen.py`) is never copied into the testbed stage, so a change to it can't alter the SUT digest.
- **Reproducible builds into OCI archives:**
  `docker buildx build --provenance=false --sbom=false --build-arg SOURCE_DATE_EPOCH=0 --output type=oci,dest=.safeops/images/<image>.tar,name=docker.io/library/<ref>,rewrite-timestamp=true`, then `kind load image-archive`.
  - **Why reproducible:** without it, Docker Desktop's provenance attestation and build timestamps gave a new digest on every build, even when every step was cached. That would have made "a measurement-code change must not alter the SUT digest" untrue in practice.
  - **Why archives instead of Docker's image store:** the `docker` exporter on Docker Desktop's containerd store rejects `rewrite-timestamp` ("conflicts with unpack") whenever layers actually need building. The OCI archive exporter has no unpack step, and loading it straight into kind keeps Docker's store out of the path entirely.
  - **Three digests, all content-addressed and reproducible.** Found live on 2026-11-19; `.safeops/images.json` records all three:

    | Digest | What it is | Seen as |
    |---|---|---|
    | config digest | the image ID | printed by `buildx --quiet`; containerd's IMAGE ID |
    | manifest digest | the image manifest | the entry in the archive's `index.json` |
    | **index digest** | sha256 of the archive's `index.json` | **what running pods report** in `status.containerStatuses[].imageID` |

    **The run record's `image_digests` (Milestone 5) uses the index digest**, because it's what the cluster actually reports for the running container. The index carries the content-derived tag as an annotation and a fixed creation time, so it's just as reproducible.
  - `cluster-up` fails unless every testbed and loadgen pod reports exactly the built index digest.
  - Verified on 2026-11-19 with this exact command:
    - two builds of the same inputs give byte-identical archives;
    - editing `loadgen.py` and `window.py` leaves the testbed digest unchanged (`sha256:b9e6c409…` before and after) while the loadgen digest changes;
    - editing `service.py` changes the testbed digest.
- **Provenance:** tags are content-derived (`safeops-testbed:<12 hex of sha256 over that image's inputs>`), for readability only. Run-record provenance (`image_digests`, Milestone 5) stores the real OCI `sha256:` digest that running pods report. Bootstrap verifies that pods report the built digest and writes `.safeops/images.json`. All four testbed Deployments share one digest, which is expected: behavior differs through version-controlled config.
- **Pull policy:** `imagePullPolicy: IfNotPresent`, with images loaded into kind. A nonexistent tag (`bad-image-tag`) then produces `ImagePullBackOff`, as §9.6 says. `Never` would produce `ErrImageNeverPull` instead.
- **Dependencies:** pinned with hashes in `testbed/requirements-*.txt` (compiled by `uv pip compile` for linux/aarch64) and installed with `--require-hashes`. These are testbed image dependencies, not SafeOps runtime dependencies.

## Memory working set

Each service holds `CACHE_MB` (64, set in the pod template) of touched, resident memory. That gives `memory-limit-low` (Milestone 11) a deterministic working set, so a limit set below it causes an OOM kill reliably. It isn't a fault hook: no code path changes behavior when a fault is active.

## Postgres

- `postgres:16-alpine`, pinned by digest in `config/cluster.yaml`. One replica, a 1 Gi PVC (`data-postgres-0`, kind's `standard` StorageClass), and a headless `postgres-svc`.
- **Password:** `cluster-up` generates a random one (`secrets.token_urlsafe(24)`) into the Secret `postgres-credentials`. It is never printed, logged or written to disk. SafeOps identities have no Secret access (§4); the Secret exists so Milestone 6 can prove that.

## Topology values the design does not give (approved 2026-11-19)

- `gateway` and `payment` get the same ranges as `orders` and `inventory`: replicas hard [0, 20] / safe [2, 6]; memory hard [64, 8192] / safe [256, 2048] MiB; CPU hard [10, 8000] / safe [100, 2000] millicores.
- All four Deployments start at **2 replicas**, the safe floor, matching §11.7's example where the fault drops 2→1 and the agent goes 1→0.
- Containers request **256 MiB / 100m** with limits of **512 MiB / 500m**. Both requests and limits sit inside the safe range (0003 F4), which `tests/test_testbed_manifests.py` checks.
- Names: Deployments are `<svc>`, Services are `<svc>-svc`, plus the `postgres` StatefulSet, `postgres-svc`, and PVC `data-postgres-0`.
- **The ownership check covers the §11.1 projected kinds** (Deployment, Service, ConfigMap, StatefulSet, PVC) in both directions. It skips objects Kubernetes or a controller created on its own: anything with `ownerReferences`, and `kube-root-ca.crt`. Secrets aren't projected and aren't in the topology.

## Layout

`testbed/` holds the app source, Dockerfile and pinned requirements. `manifests/{system,testbed,workload,helm}` hold the Kubernetes YAML and Helm values, with `${UPPER_CASE}` placeholders filled strictly from `config/cluster.yaml`; an unknown placeholder is an error. `kind/cluster.yaml` is a single control-plane node.

## Amendment, 2026-11-19: every running image is recorded by digest (second review, P1)

- **The gap:** the Chaos Mesh and Prometheus charts reference their images by tag (for example `ghcr.io/chaos-mesh/chaos-mesh:v2.8.8`, `quay.io/prometheus/prometheus:v3.16.0`). The charts themselves are pinned by sha256 (0006), but a re-pushed image tag would silently change the fault backend or the agent's telemetry.
- **The fix:** `cluster-up` now writes `.safeops/images.json` as:
  - `{"built": …}`: the SafeOps images, with all three digests;
  - `{"running": {image: [imageID…]}}`: every container and init container in every SafeOps namespace, as the pods report it.
- **For Milestone 5:** run-record provenance takes third-party image digests from `running`. Pinning chart images by digest is possible through chart values; it's deferred unless a digest is ever seen to drift between rebuilds.
