# 0023 — Milestone 6: read-only tools, safeops-reader, run record version 2

**Status:** Accepted (2026-11-22), Milestone 6.
**Design refs:** §3, §4, §6, §11.3, §11.8, §14 (Milestone 6), §15 Rules 1, 12, 18, 21, 27; decisions 0009, 0010, 0013, 0017 §4, §6, §7, 0018, 0020–0022

**Acceptance (§14, verbatim):** "Outputs bounded; no tool can mutate under hostile args; `pods/log` granted `get` only."

Everything below is an implementation choice the design leaves open, or a structural consequence of one. The design document is unchanged.

## 1. Scope: the eight §11.3 tools

- The Milestone 6 brief named four tools. §11.3's table has eight: those four, plus `get_deployment`, `get_rollout_history`, `get_configmap` and `get_service`. No decision split them across milestones.
- **Resolved by the user (2026-11-22): all eight**, because the design wins.
- There is no registry, discovery mechanism, or model-facing description layer. That belongs to the agent (Milestone 9).
  - The only table is `contracts.CONTRACTS`: tool name → (argument model, result model).
  - The run-record validator needs it to check stored steps; nothing else does.

## 2. Modules

| Module | Holds |
|---|---|
| `safeops/tools/kube.py` | the seven Kubernetes tools |
| `safeops/tools/metrics.py` | `get_metrics`, its five PromQL templates, the window plan |
| `safeops/tools/contracts.py` | argument and result models, bounds, `ToolError`, the trajectory envelope, `invoke`, `step_problems` |
| `safeops/tools/canonical.py` | non-finite normalization, canonical JSON, `result_digest` |
| `safeops/tools/reader.py` | `ToolContext` (reader client, topology, evidence floor, clock); typed read helpers |
| `safeops/environment/clock.py` | the clock probe and its derivations (harness side) |
| `manifests/system/safeops-reader.yaml` | the reader's ServiceAccount, Roles, RoleBindings |

- Every tool is a plain function, `tool(ctx, **args) -> dict`.
- It validates its arguments before any read, reads through the reader client only, and projects the response onto its contract.
- It orders the projection deterministically, bounds it, normalizes it, and returns it.
- `invoke(tool, ctx, args)` returns the stored envelope: `{"status": "ok", "data": …}` or `{"status": "error", "error": {"kind", "message"}}`.

## 3. safeops-reader: identity and RBAC

- **ServiceAccount:** `safeops-reader`, in `safeops-system`, so it survives a testbed rebuild.
- **Credential:** bootstrap issues a time-limited TokenRequest token for it.
  - It has the same lifetime as the harness token.
  - It lives at `reader_kubeconfig_path` / `reader_token_meta_path` in `config/cluster.yaml`.
  - `cluster-down` removes it.
- **Testbed Role:** exactly the §4 list.
  - `get`, `list` and `watch` on pods, events, services, configmaps, endpointslices, deployments, replicasets and statefulsets.
  - `get` only on `pods/log`.
- **Monitoring Role:** `services/proxy`, verb `get`, on resource name `prometheus-server:80`. The proxy goes by port: the Service's port is named, so the bare name resolves to no endpoints (found live; §15).
  - §3 draws a read-only edge from the tools to Prometheus. This is that edge.
  - GET reaches only the read API. Prometheus' lifecycle endpoints need PUT or POST, which would be `update` or `create` on the proxy; neither is granted.
- **Nothing else:**
  - no Secrets;
  - no exec, attach, port-forward, eviction or scale;
  - no proxy in the testbed;
  - nothing in the workload namespace, its proxy included (0009);
  - nothing in the chaos, system or kube-system namespaces;
  - nothing cluster-scoped.
- **Client side, `Phase.READER` in `safeops/cluster.py`:**
  - it uses the reader's own credential and refuses the admin or harness kubeconfig;
  - `check_object` refuses every mutation in this phase;
  - raw calls may only GET `…/services/prometheus-server:80/proxy/api/v1/{query,query_range}?…`, with no `..` or `#` anywhere in the path.
  - A static test asserts that tool code calls only `read`, `raw` and `cfg` on the client, and never names `Phase.RUNTIME`, `Phase.BOOTSTRAP`, the harness credential or `WorkloadProxy`.
- **Checked both ways:**
  - `bootstrap.reader_rbac_expectations` lists 101 `can-i` expectations: 26 allowed, the rest denied, including the bare-name proxy. `cluster-up` runs them.
  - The live tests run them again with the reader's own token.
  - Server-side dry runs prove the API server refuses the reader's writes.

## 4. Arguments are untrusted

- **Strict types:** a bool is not an int, `"5"` is not 5, 5.0 is not 5.
- **Closed enums:**
  - `severity`: `debug`, `info`, `warn`, `error`;
  - `metric`: the five names in §6.
- **Kubernetes name grammars:**
  - `namespace` and `service` are DNS-1123 labels;
  - object names are DNS-1123 subdomains.
- **Checked at call time:**
  - `namespace` must be the testbed;
  - `service` must be a topology service.
- **Ranges:**
  - `last_minutes` and `window` are 1–60, in minutes; Prometheus keeps 2 h (0010);
  - `max_lines` is 1–200.
- **Out of range is rejected, never clamped.** `max_lines=201` or `10000` is `invalid_argument` before any read. A valid `max_lines=100` with more matching lines returns 100 and says so (§7).
- **An argument is never its own argv element, a PromQL fragment, a path or a selector expression.**
  - A validated name appears only as the value in `metadata.name=<name>`, `app=<service>` or `kubernetes.io/service-name=<name>`.
  - The hostile-argument test checks this for 30 hostile values, every argument of every tool: flags, other namespaces, shell, templates, JSONPath, selector injection, paths, URLs and wrong types.
  - It also checks, with an independent copy of the grammar, that any value reaching a selector is a valid Kubernetes name.

## 5. Evidence floor: the current incident only

- **Resolved by the user: the floor applies to all three time-windowed tools** (events, logs, metrics), not only to metrics as 0017 §4 required.
- **Why:** Pods, Events and Prometheus series outlive a reset, so without a floor the agent could read an earlier run's fault. That is the same leak 0013 closed for rollout history.
- `ToolContext.not_before` is the current run's load start.
  - Events and logs use `since = max(now − last_minutes, not_before)`.
  - The first metric point is no earlier than `not_before + range_s`, so no `rate()` range reaches back before the load.
  - An incident younger than one rate range has no points yet, and no query is sent.
- Metrics are rates, ratios or gauges, never raw counters (0017 §4).

## 6. The tools, their bounds and order

| Tool | Result | Bound | Order |
|---|---|---|---|
| `get_pods` | name, phase, ready, terminating, restarts, start time, per-container state, reason and exit codes. Status only. | 50 pods, 10 containers | by name |
| `get_events` | last and first seen, type, reason, involved kind and name, message (1024 chars), count | 50 (§11.3) | the 50 most recent kept, listed oldest first; ties broken on kind, name, reason, type, message, count, never on API order |
| `get_logs` | timestamp, pod, container, level, text (2048 chars) | `max_lines` ≤ 200 (§11.3); each stream read with `--tail=1001` and `--since-time` | the newest `max_lines` matching lines, listed oldest first, by (time, pod, container, stream position) |
| `get_metrics` | `start`, `end`, `step_s`, `range_s`, unit, points | 60 points (§11.3), by construction | time |
| `get_deployment` | replicas, current revision, per container: image, env, envFrom, requests, limits, probes | 10 containers, 100 env | containers by name |
| `get_rollout_history` | the Deployment's owned ReplicaSets: revision, name, created, replicas, ready, pod template | 5 revisions (§11.3) | the five highest, listed oldest first |
| `get_configmap` | entries: key, value (4096 chars), length; binary keys named only | 64 keys | by key |
| `get_service` | selector, ready endpoint count, total endpoint count, from EndpointSlices (§4, §11.3) | 32 selector entries | n/a |

- **Metric surface. Resolved by the user: five service metrics.**
  - The names are `request_rate`, `error_rate` (the 5xx fraction; status `code=~"5[0-9][0-9]"`), `latency_p95_seconds`, `cpu_usage_cores` and `memory_working_set_bytes`.
  - Each is a fixed `string.Template`: the namespace, the service's `app` label, a pod-name pattern built from `config/topology.yaml`, and a range.
  - The workload generator's `loadgen_*` series are not exposed at Milestone 6.
- **Metric window plan:**
  - `step_s = max(5, ceil(span / 59))`;
  - `range_s = max(20, the window's nominal step)`.
  - At most 60 points for every window and every floor; a test checks all 360 combinations.
- **Severity:** `severity` is a minimum level.
  - A line's level comes from the testbed's JSON `level`, or from a Postgres prefix (`LOG`, `WARNING`, `ERROR`, `FATAL`, …).
  - A line with no recognizable level (a traceback, say) is kept at every severity, with `level: null`. Dropping possible error evidence is worse than showing noise.
- **Ready endpoints** means `conditions.ready is true`, as the canonical snapshot counts it. The tool can't import the snapshot module (the wall), so the small count is written again here, with the same semantics.

## 7. Failure is never an empty result

- **Error kinds:**
  - `invalid_argument` (before any read);
  - `credential`, `guard`, `command_failed`, `timeout` (from `CredentialError`, `GuardError`, `CommandError`, `TimeoutExpired`);
  - `malformed_response` (not JSON, not a list, a shape the contract can't represent, a projection that fails its own contract).
- **Answers that are not failures:**
  - **Not found:** a field-selected list comes back empty, and the result says `found: false`. That is an answer from the API server.
  - **A container that never started:** it has no log stream, so it is listed in `unavailable`. A started container whose read fails fails the whole call.
  - **No series:** Prometheus returned an empty matrix, and the result has zero points.
- **A bug is not a tool error.** Any other exception propagates; it is never shown to the agent as a result.

## 8. Non-finite numbers

- **Representation:** `{"non_finite": "NaN" | "+Inf" | "-Inf"}`.
- It is strict JSON and deterministic. It differs from `null`, from `0` and from every finite number. A later reader (feature extraction) tells undefined apart from "measured 0" by structure, not by convention.
- Prometheus' string values `"NaN"`, `"+Inf"`, `"Inf"` and `"-Inf"`, and any other value that parses as non-finite, become the tag at the `get_metrics` boundary.
- `normalize()` then applies the same rule recursively to every tool result.
- 0.0 stays 0.0, and a non-finite value is never turned into 0, dropped, or made to empty the call.

## 9. Canonicalization and `result_digest`

**Pipeline:**

```
raw response -> projection (contract fields only) -> deterministic order -> bounding/truncation
-> normalize() -> envelope {"status", "data" | "error"} -> canonical_json() -> result_digest
```

- `canonical_json` is `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`.
- `result_digest` is `"sha256:" + sha256(canonical_json(result).encode("ascii"))`.
- `trajectory[].result` is the envelope, and the digest is taken over exactly that value: the one the agent saw and the record stores.
- A non-normalized value can't be hashed: `allow_nan=False` raises.

## 10. Evidence agreement in tool results

Every derived field is checked against what is stored beside it. The checks run when a tool builds its result; a projection that fails one is `malformed_response`. They run again for every stored step in a version-2 record (`contracts.step_problems`).

- the digest is recomputed over the stored result;
- the envelope has exactly one of `data` and `error`, and the status matches;
- a success has valid arguments, and its result echoes every one of them;
- a failure other than `invalid_argument` was attempted only with valid arguments;
- each count equals the length of its list;
- each total is at least its count;
- `truncated` is set exactly when the total exceeds the count, and a truncated list is full;
- logs: `line_count = min(matched_count, max_lines)`;
- each text-truncation flag agrees with its length;
- a pod's `restarts` equals the sum over its containers;
- a ConfigMap entry is redacted exactly when its key names a secret; a redacted entry shows no value or length;
- a metric `unit` matches its metric;
- metric points lie on the `start + k * step_s` grid, and no more than 60 fit;
- `since` is never earlier than `last_minutes` allows;
- every event and line is at or after `since`, and at or above the requested severity;
- lists are in their stated order.

## 11. No evaluator state through outputs

The import wall is necessary, not sufficient; outputs were audited field by field.

- **Projection, never raw objects.** No labels other than a Service's selector, and no annotations except the Deployment revision. No `managedFields`, which would show `kubectl-replace` from the injector, no ownerReferences, UIDs, pod IPs, image IDs, or container IDs.
- **Harness events are filtered.** An event about a `chaos-mesh.org` object, or emitted by a component matching `chaos`, never reaches a result. Those are the injection mechanism itself (Rule 1; §11.1 "explicitly ignored").
- **Secrets.** There is no Secret access at all. An env var or ConfigMap key whose name matches `password|passwd|secret|token|credential|api[-_]?key|private[-_]?key` is redacted, and so is its length. `secretKeyRef` shows the Secret's name and key, never a value.
- **Prometheus.** Every template aggregates (`sum`), so no series labels reach a result. No harness series is queried: the loadgen series are not exposed, and the generator's HTTP metrics are already dropped at scrape (0010).
- **Kept on purpose:** evidence a real operator would see (an env value the fault broke, a ReplicaSet of the faulty revision, an event saying pods were scaled, an app's own error log). Production-like evidence is allowed; ground truth is not.
- **The floor.** Events and logs from an earlier run never appear (§5).

## 12. Run record version 2

- **Why it exists:** 0017 §7 requires a structural addition to bump `schema_version`. Milestone 6's clock readings are that addition. This is a forward addition, not a correction to version 1.
- **Version 1 is unchanged and strict:**
  - `STAGE_PRODUCES_V1` is the original table;
  - version 1's `DiscardReason` refuses the two new stages;
  - a version-1 record refuses the clock fields, whether null or not;
  - three real version-1 records, written by the Milestone 5 writer before this change, are frozen in `tests/fixtures/records_v1/` and still validate as `RunRecordV1`.
- **Every new record is version 2.** `SCHEMA_VERSION = 2`; `SCHEMA_VERSIONS = {1: RunRecordV1, 2: RunRecordV2}`; dispatch is unchanged.
- **Version 1's rules carry over.** `RunRecordV2` subclasses `RunRecordV1`, inherits its validator, and `record_problems` runs every version-1 rule for both. Version 2 swaps in its own stage table and core chain, and adds its clock and stored-step checks.
  - Tests prove that representative rules still fire on version 2: cause and stage, `pre_run_dirty`, proposal and execution, cohort G, harm and its violations, monotonic presence, the trajectory digest, steady-state arithmetic, token totals, and non-finite JSON.
- **Clock structure** (top level; each nullable, presence governed by stage):

  ```
  clock_before_load, clock_after_measurements:
      {host_before, host_after, server_created, offset_s}
  clock_change:
      {change_s, interpretation: "change_detected" | "no_change_detected"}
  ```

- **Two new `RunStage` members:**
  - `CLOCK_BEFORE_LOAD` (`"clock_before_load"`) sits immediately after `PRE_RUN_CLEAN_STATE`, before `START_WORKLOAD`. It is in `PRE_AGENT_STAGES`.
  - `CLOCK_AFTER_MEASUREMENTS` (`"clock_after_measurements"`) sits immediately after `HARM`, last. It is in `POST_AGENT_STAGES`.
  - The two tuples still partition the enum exactly.
  - `stage_phase` classifies both by membership; an unclassified stage still raises.
- **`STAGE_PRODUCES` governs presence:**
  - `CLOCK_BEFORE_LOAD → clock_before_load`;
  - `CLOCK_AFTER_MEASUREMENTS → clock_after_measurements, clock_change`;
  - the strictly-before function is unchanged.
  - So a dirty pre-run discard has neither reading. A first-probe failure has neither. A discard at `START_WORKLOAD` through `HARM` has the first only. A second-probe failure has the first only. A completed run has both, and the change.
  - The core chain for version 2 adds both readings and the change.
- **Causes, explicit (`CLOCK_STAGE_CAUSES`):**
  - `clock_before_load`: `infrastructure_failure`;
  - `clock_after_measurements`: `infrastructure_failure`, or `operator_abort` under R2.
  - No new discard cause: §15 Rule 5 names infrastructure failures as discardable, and the probe is harness infrastructure.
- **Stage before the call:** the runner sets each probe's stage before calling `read_clock`, as `_ResetProgress.read` names its step first (0022). A failed probe is never attributed to the stage before it.
- **Derived values are evidence-checked:**
  - each `offset_s` equals its timestamps' offset exactly;
  - `server_created` has whole-second resolution;
  - `change_s` and `interpretation` are recomputed from the four stored readings;
  - `clock_change` is present exactly when both readings are.

## 13. The clock probe

- **Probe kind: a ConfigMap** named `safeops-clock-probe-<16 hex>`, with labels `app.kubernetes.io/part-of: safeops` and `safeops.io/role: clock-probe`.
  - It is created through the guarded runtime client, `Kubectl.create_returning`, the one new client method.
  - experiment-controller already creates and deletes ConfigMaps in the testbed, so no RBAC changed.
  - A leftover probe is not a hidden one. Reset deletes it like any undesired ConfigMap, and verify reports it.
- **One reading:**
  1. host wall clock immediately before the create;
  2. the create returns the stored object, with the server-populated `creationTimestamp`;
  3. host wall clock right after;
  4. delete, then confirm it is gone with a fresh field-selected list.
  - Any failure to create, read or remove it raises `ClockProbeError`. A reading is never synthesized.
- **Arithmetic, in integer microseconds:**
  - offset: `server − (before + after) // 2`;
  - the true offset lies in `[server − after, server + 1 s − before)`, because `metav1.Time` truncates to whole seconds;
  - change: the difference of the two offsets.
- **Interpretation. Resolved by the user: an uncertainty test with no threshold.**
  - `change_detected` when the change's interval excludes zero: the offset moved by more than the two readings' resolution and latency can explain.
  - Otherwise `no_change_detected`.
  - Milestone 12 adds its flagging bounds on top.
- **When the readings are taken:**
  - the first after the pre-run clean check, before the load and the incident begin;
  - the second right after `HARM`, inside the measured block, before the cleanup's reset.
  - The probe is never present during the agent, a snapshot or harm measurement.
  - The load is still flowing at the second reading. Stopping it is part of the cleanup, and nothing measures it any more. So 0017 §6's "not present during the workload" is read as "during any measured part of the run".
- **A failed second reading discards the run** (post-agent `infrastructure_failure`, with its data kept).
  - §11.1 keeps runs whose measurements are complete. Here the second reading is part of the run's measurement record: without it the run's timeline can't be checked at Milestone 12. Measurements are complete only once it exists.
  - This was the brief's explicit requirement.

## 14. Information wall

- `safeops.cluster` joins the wall's protected list as a neutral module the tools consume, so it must stay neutral.
- A test proves the real tools reach the reader client and nothing forbidden.
- Negative fixtures prove a "neutral" helper that imports harness code doesn't launder the import (environment, experiment, faults, verify).
- No evaluator logic moved into a neutral module. The endpoint count was written again (§6), not extracted from the snapshot module.

## 15. Verification and the adversarial review

- **Offline:** 1578 passed; lint and format clean; the repository policy checker passes.
- **Mutation checks: 34, all killed.** They cover the brief's 32 classes; truncation (tool and validator) and probe-stage attribution (one per probe) have two mutants each:
  - a scope guard removed;
  - unrestricted name input;
  - a failed read turned into empty success;
  - each of the three caps removed;
  - clamping;
  - truncation metadata, at the tool and at the validator;
  - a non-canonical digest;
  - digest agreement off;
  - NaN escaping;
  - NaN turned into 0;
  - no ordering before bounding;
  - a count disagreeing with its list;
  - harness events leaking;
  - a forbidden import;
  - version 1 accepting a clock field;
  - version 2 skipping a version-1 validator;
  - the writer emitting version 1;
  - an offset unvalidated;
  - a wrong change;
  - a wrong interpretation;
  - a discard carrying its own stage's reading;
  - a later stage losing an earlier reading;
  - a one-reading change;
  - reading 1 on `START_WORKLOAD`;
  - reading 2 on `HARM`;
  - either probe stage dropped from its partition;
  - `stage_phase` misclassifying;
  - no explicit probe causes;
  - the stage set after a successful probe, for each probe.
  - Attribution mutants are also caught by the semantic tests, not only by the table-equality test.
- **Live:**
  - `make cluster-test`: 65 passed after a rebuild through `cluster-down` / `cluster-up`.
  - After the fixes below, `tests/cluster/test_tools_live.py` and `test_records_live.py` passed again: 37.
  - `cluster-up` ran the 101 reader expectations.
  - The reader's own token was refused every write in server-side dry runs, and every out-of-scope read.
  - All eight tools passed their contracts on real responses.
  - Real load produced current-incident rates.
  - A real run took its first reading with the load stopped, before `start_workload`, and its second after `harm`, with the load still flowing, before the cleanup's reset; offsets were within ±5 s.
  - `safeops verify` reported CLEAN.

**Adversarial review findings, fixed in this change:**

1. **(Live) The Prometheus proxy needs the port.** The Service's port is named, so `services/prometheus-server/proxy` resolved to no endpoints. The path, the RBAC resource name and the expectations now use `prometheus-server:80`, and the bare name is asserted denied.
2. **(Live) The real reader guard refused `code=~"5.."`** (a `..` in the path). The offline fake had bypassed the guard. The regex is now `5[0-9][0-9]`, the guard stays strict, and a new offline test sends every template, for every service, through the real `Kubectl.raw` guard.
3. **`get_logs` dropped streams without saying so** (more than 5 containers in a pod, or more than 20 unavailable streams to name). `scan_limited` now admits every stream not fully read.
4. **Offline tests deleted the real reader credential.** `bootstrap.down` now removes the reader's files, but two offline teardown fixtures redirected only the admin and harness paths into a temporary directory. There is now one helper, `tests.conftest.scratch_cluster_config`, which moves every path key. An autouse guard fails any offline test that changes the real `.safeops/`, and a test asserts that every path key is redirected. The cluster was rebuilt to restore the credential.

**Checked, no change:**

- No harness object or label reaches an output (§11).
- No argument reaches kubectl or PromQL except a validated value in a fixed position (§4).
- Every cap holds, by construction and by validation.
- Zero and the non-finite tag are distinct.
- The digest covers exactly the stored envelope.
- Version 2 inherits every version-1 rule; version 1 refuses version-2 fields and stages.
- The writer emits version 2 only.
- Readings are attributed to their own stages.
- No Milestone 7–9 structure was added.

**Accepted as is:** pod and container reasons, event types and kinds are Kubernetes identifiers, cut at 128, 32 and 63 characters without a flag. Real values are far shorter.

## 16. Carried forward

- **Milestone 9 (agent loop):**
  - The runner builds `ToolContext(not_before=<the load start>)`, using the host wall clock taken immediately before `start_workload`.
  - Each step's `result` is `invoke(...)` and its digest is `result_digest(result)`, exactly as stored.
  - Decide which error kinds end the run as `infrastructure_failure` rather than being shown to the agent: at least `credential` and `guard`.
  - Hash the eight contracts into `tool_schema_hash`.
- **Milestone 11:**
  - ~~Check `get_logs` on a really crash-looping container~~: done at the Milestone 6 closure (closure amendment).
  - `memory-limit-low`'s durable evidence comes from `get_pods` (termination reason and exit code) and `get_events` (0010).
  - **Event messages (the external review's F2/F3 concern; not decision 0003's F2/F3).** When the faults whose mechanisms make the kubelet or a controller emit events are implemented or exercised, inspect the actual events those mechanisms generate. Verify that normal kubelet and controller event messages can't reveal evaluator-only process, container or resource identifiers. Add filtering only from concrete implemented identifiers and observed messages, never from generic substring blacklists: `chaos`, `fault`, `safeops`, `injected`, `error` or `timeout` would remove legitimate operational evidence. Today's filter is structural only (§11: the `chaos-mesh.org` group and `chaos` emitters).
- **Non-blocking, when pilot numbers exist:** `get_rollout_history` reads every ReplicaSet in the namespace, then filters by controller owner before any projection, so a foreign ReplicaSet never reaches `_containers()`. Narrow the read server-side (a label selector serialized from the Deployment's) only if pilot object counts or timings show the namespace-wide read matters. Serializing `matchLabels` and `matchExpressions` correctly isn't worth it before then.
- **Milestone 12:**
  - The clock offset and change bounds, from measured values (0017 §6).
  - `not_before` is host time, while events, logs and samples carry cluster time; their alignment is that milestone's to judge.
- **Milestone 7:** nothing from this milestone.

## Amendment, 2026-11-22: adversarial review after commit `b16d913`

Four bugs were found and fixed. No record field outside the stored tool results changed; the run-record schema version is unchanged (version 2 has no finalized records yet).

1. **`error_rate` returned no points for a healthy service.** Confirmed live: at a steady 10 requests/s, `request_rate` had 6 points and `error_rate` had 0, for gateway and orders.
   - **Cause:** the 5xx counter series exists only after a first 5xx, so the numerator was an empty vector and the division returned nothing. The agent saw "no data" exactly when the error rate was 0. The live test's per-point loop passed vacuously over an empty list.
   - **Fix:** the numerator is `(sum(rate(…5xx…)) or vector(0))`.
     - No errors read 0.
     - No traffic reads 0/0, the non-finite tag.
     - A service that never served a request still has no series, so no points.
   - **Test:** the live test now requires points, and requires the last one to be exactly 0 under steady, fault-free load.
2. **`get_logs` lost the lines from before a restart inside the window, while claiming a complete read.** `kubectl logs` serves only the latest instance.
   - **Fix:** a running container whose previous instance ended inside the window has that instance read too, with `--previous` and the same hard tail and `--since-time`.
   - Each line now carries `instance: "latest" | "previous"`.
   - **`scan_limited`** is set when an instance that may have logged inside the window can't be read. That is an instance older than those read, which the kubelet no longer keeps, detected from the restart count and the oldest read instance's start time.
   - **A `--previous` read that fails as `command_failed`** (the kubelet already discarded it) is admitted the same way. Every other failure still fails the call.
   - **A container waiting to restart** is read once: `kubectl logs` already serves its last instance, so reading `--previous` too would duplicate it.
3. **A failed first clock reading stopped a load the run never started.** `CLOCK_BEFORE_LOAD` sits inside the stop-on-failure block, which before Milestone 6 began at `start_workload`, where the busy check lives.
   - **Fix:** the runner sets `load_requested` just before calling `start_workload`, and stops the load on failure only once it is set. It is set before the call, so a lost start response still stops the load (fix list, item 4).
   - A failure before that point leaves the generator alone; it may be running someone else's load (0009, W5).
4. **Event counts and `first_seen` could reach before the window.** An event is kept when its last occurrence is inside the window, but Kubernetes aggregates repeats, and the count can't be split.
   - **Fix:** each event carries `began_before_since`, true exactly when `first_seen < since`. The result validator recomputes it, so a flag that disagrees with its own evidence is rejected.
   - The count is kept as reported. In practice this happens within a run, for example readiness failures during the pre-run reset, because reset recreates the workloads with new UIDs.

**Verification:**
- Offline: 1585 passed.
- Mutation checks: 42, all killed: the original 34, plus 8 for these fixes:
  - `error_rate` without `or vector(0)`;
  - the previous instance never read;
  - an unreadable older instance not admitted;
  - older instances judged from the wrong restart count;
  - a discarded previous instance failing the call;
  - the load stopped before it was requested;
  - the events flag never set;
  - the events flag unchecked.
- Live: `tests/cluster/test_tools_live.py` and `test_records_live.py`, 37 passed, including the strengthened `error_rate` check; `safeops verify` CLEAN.
- **Not proven live:** the `--previous` read. Restarting a container needs exec, which no SafeOps identity has, and the cluster isn't damaged just to test it. The Milestone 11 carry-forward (§16) now covers it: verify `get_logs` on a really crash-looping container, both instances, and the waiting case.

## Closure amendment, 2026-11-23: log framing and CrashLoopBackOff on the pinned kubelet

Milestone 6's final pass. The clock policy, schema, stages, RBAC, evidence floors and metrics are unchanged.

**Log framing (a bug).** `get_logs` split `kubectl logs --timestamps` output with `str.splitlines()`, which also breaks on nine characters kubectl doesn't use as delimiters: `\r`, `\v`, `\f`, `\x1c`, `\x1d`, `\x1e`, `\x85`, `\u2028`, `\u2029`.
- **Effect:** one valid record containing any of them, a bare `\r` from progress output in practice, became several pieces. Only the first carried the timestamp, so the whole call failed as `malformed_response`.
- **Fix:** records are framed on literal `\n` only (`kube.split_records`). Exactly one element is dropped: the empty one after the final `\n`, which is a delimiter artifact and not a record. That happens before the `LOG_SCAN_LINES` comparison and the record count.
- **Strictness is kept.** An empty element in the middle is not dropped; nor is any other record without a valid timestamp prefix. Both still fail as `malformed_response`. A timestamped record with an empty message is a real record.
- **Contract: a `\r` before kubectl's `\n` is application text and is kept.** `"<ts> message\r\n"` yields the text `"message\r"`, which counts toward the canonical result and its digest. This deliberately differs from the old behavior, which silently changed application data.
- **Tests:** all nine separators, the CRLF case and the digest, the scan-cap boundary (exactly `LOG_SCAN_LINES` records plus the final newline is not limited; one more is), malformed records, and an empty message.

**CrashLoopBackOff, observed on the pinned kubelet (Kubernetes v1.37.0, 2026-11-23).** The fixture was a temporary Deployment running the loaded testbed image. It printed a unique marker and exited 3, and was observed through the reader's own token. The status was read before and after the log reads, and the observation was accepted only when the same terminated instance held throughout (the first attempt raced a restart and was discarded).
- **Status:**
  - `restartCount: 4`;
  - `state.waiting.reason: CrashLoopBackOff` ("back-off 1m20s …");
  - `lastState.terminated`: `exitCode 3`, `reason Error`, a `containerID`, and `startedAt`/`finishedAt`;
  - `ready: false`.
- **Plain `kubectl logs` while waiting** returns the last terminated instance: its marker carried that instance's start time.
- **`--previous` while waiting** returns the very same instance, byte for byte.
- **Earlier instances** (three earlier crashes) are unavailable in every mode: the kubelet keeps only the last dead container.
- **A container with no previous instance** gets `Error from server (BadRequest): previous terminated container "<c>" in pod "<p>" not found`.

**The SafeOps rule, which needed no change for the waiting case:**
- **Waiting:** one plain read; it already serves the crashed instance, and reading `--previous` too would duplicate it. Lines say `instance: "latest"`.
- **Running, with a previous instance that ended inside the window:** a plain read plus `--previous`. That instance is skipped when it ended before the floor.
- **Terminated:** one plain read.
- **Never started:** no read; the stream is listed in `unavailable`.
- **`scan_limited` is true** when an instance older than those read may have logged inside the window: there are more restarts than instances read, and the oldest read instance started after `since`. It is also true when a `--previous` read gets exactly the no-previous answer above. Otherwise it is false.
- **Live:** the waiting fixture returned its marker as `latest`, with `scan_limited: true` for the three unreadable earlier crashes.

**One production change from the observation (a tightening).** The first amendment treated *any* `command_failed` on a `--previous` read as history unavailable. Only the observed no-previous answer is now (`kube._NO_PREVIOUS`). Every other failure is a typed failure, as on any read: forbidden, "waiting to start", a runtime error, a timeout. A live test pins the form against the real kubelet.

**Also settled, no change:**
- `get_rollout_history` filters by controller owner before projecting; a regression proves a foreign or unowned ReplicaSet with no spec never reaches `_containers()`.
- The `clock.py` docstring now matches §13 (the second probe may coexist with load still flowing until cleanup, never with a measured part of the run). The runtime order is untouched.
- The carry-forwards for event messages and the ReplicaSet read are in §16.

**Verification:**
- Offline: 1612 passed, with 27 new regression cases; lint, format and repository policy clean; information wall 19 passed; no offline test touched the real `.safeops/`.
- Mutation checks: 46 attempted, 46 killed: the 42 existing (one re-pointed: R2d, whose guard moved into the narrowed no-previous condition) and 4 new:
  - `splitlines()` restored: all nine separator tests fail;
  - the synthetic trailing element kept: the exact-`LOG_SCAN_LINES` test fails;
  - a trailing `\r` stripped: the CRLF test fails;
  - any `--previous` command failure treated as unavailable.
  - No waiting-branch mutant: that code didn't change.
- Live: `test_crashloop_live.py` (2), `test_tools_live.py` and `test_records_live.py`, 39 passed. `safeops verify` was CLEAN before and after every fixture, and at the end. `manifests/` and `config/` are unchanged, so RBAC is unchanged.
