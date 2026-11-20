# 0017 — Requirements carried from Milestone 4 into Milestones 5, 6 and 11

**Status:** Accepted (2026-11-19), recorded at the Milestone 4 closeout; §6 added at the final review. **Recorded, not implemented.** Each item names the milestone that must satisfy it before that milestone's acceptance criterion counts as met.
**Design refs:** §5, §7.1, §7.2, §11.1, §11.3, §11.8; decisions 0009, 0011, 0013, 0015

## 1. BLOCKING for Milestone 5: a verifier or harness exception after the agent starts must still produce a record

**Where:** `safeops/experiment/runner.py`, the `except BaseException` at **line 137**.

- That handler now spans the measurements: from `start_workload` through `self.effectiveness.verify` (line 135) and `self.harm.check` (line 136).
- It stops the load and re-raises. **Nothing after it runs:** no post-run reset, no `records.finalize` (line 164), no `_recover_if_dirty` (line 183).

**Reproduced offline on 2026-11-19.** A harm checker that raises `KeyError` produced exactly these environment calls: `reset_environment`, `verify_clean_state`, `capture_snapshot`, `capture_settled_snapshot`, `wait_stabilization_window`, `capture_snapshot`. There was no finalize, no post-run reset and no quarantine. So:
- the run is lost: no record, no trajectory, no proposal, no executed action;
- the environment is left dirty, so the next run's pre-run check discards that run as well.

Both verifiers are stubs today, so this is inert until Milestone 7. That is also when the structure is hardest to change, so the schema half is decided at Milestone 5.

**The invariant Milestone 5 must implement:**
- Once the agent has produced a trajectory or a proposal, and especially once an action has been attempted, a verifier or harness exception must produce an **`infrastructure_failure` record**.
- That record preserves all the trajectory, proposal and execution data available. `effective` and `harmful` may be unmeasured.
- The post-run cleanup and recovery then run, in Milestone 3's order: clean up, decide `reset_verified`, finalize, then recover (decision 0013, third review).
- Such a run is excluded from cohort G (§7.2), but it is **never silently lost**.

**Why it matters:** this is the same bias as Milestone 3's quarantine-before-finalize defect, one stage earlier. The runs most likely to crash the harm checker are the ones where the agent did the strangest thing. Losing them quietly biases the dataset toward well-behaved runs.

**Already right, and to keep:** failures before the agent starts are pre-run discards (§11.1). Stopping the load in that handler is cleanup, not classification (0015 amendments).

## 2. Milestone 5: the settledness condition must reach the record

`EnvironmentManager.capture_settled_snapshot` (`manager.py:403`) returns a `SettledSnapshot` with `settled`, `polls`, `elapsed_s` and `last_changes`. The runner passes the whole object to `records.finalize(postfault=…)`.

When Milestone 5 replaces the writer stub, **at least the settled/unsettled condition** must be in the record, next to `postfault_snapshot_hash`. Otherwise decision 0013's requirement ("record and flag the unsettled condition") disappears at serialization time, and an unsettled harm baseline becomes indistinguishable from a settled one. `polls`, `elapsed_s` and `last_changes` should go in as well, since they cost nothing.

## 3. Before `memory-limit-low` (Milestone 11): a symptom metric over all failed attempts

- `SUPPORTED_METRICS` (`spec.py:37`) holds only `http_5xx_rate`. `_server_error_inconsistency` (`workload.py:154`) requires 5xx ≤ failed. The metric counts only 5xx **answered inside the deadline**.
- A fault whose real symptom is OOMKilled pods produces refused connections, resets and timeouts, not 5xx responses. `server_error_responses` would stay near zero, `verify_fault_present` would report the symptom absent, and **every run would be discarded as silent non-injection while the fault worked perfectly.**
- `bad-deployment-config` is unaffected: all four variants break an upstream URL, and gateway answers 502 within the deadline.

**Requirement:** before `memory-limit-low` is added (decision 0011), define a second symptom metric over **total failed attempts** (or timeouts and no-response attempts), measured on the same fixed-denominator windows. Give it its own `ALERT_NAMES` entry, and calibrate it under decision 0016. Don't stretch `http_5xx_rate` to cover it.

## 4. Milestone 6: agent-facing workload metrics are judged over the current incident only

- The load generator's Prometheus counters, `loadgen_attempts_total` and `loadgen_attempt_duration_seconds` (`loadgen.py:28–29`), live for the whole process. They survive testbed resets, and decision 0009 lets the agent see them through Prometheus.
- The Milestone 4 fix list made `start` the run boundary for the harness's windows and `dispatched_total`. It deliberately leaves these Prometheus counters alone: resetting a Prometheus counter in-process isn't supported, and `rate()` handles counter resets anyway.

**Requirement:** agent-facing workload metrics must be evaluated over a **bounded current-incident window**. Lifetime totals accumulated by earlier runs must never become evidence for the current run. **`get_metrics(service, metric, window)` (§11.3) is the enforcement point:**
- it queries rates or increases over a window that ends now and starts no earlier than the current run's load start;
- it never returns raw cumulative counter values.

Don't redesign the loadgen metrics.

## 5. Milestone 5: a live test of the deployed load generator's run boundary

- `tests/test_m4_fixlist.py` covers the run-boundary logic offline. It uses the real `loadgen.py` and real `Window` objects, with only `prometheus_client` and `app.common` replaced by stand-ins. That is enough for the logic.
- Nothing live yet proves that the **deployed image** behaves the same way over the real HTTP and API-server-proxy path.

**Requirement:** a live test in `tests/cluster/` that:
1. starts the workload generator;
2. measures at least one window;
3. stops it;
4. starts it again;
5. reads `/stats`;
6. checks that the previous run's window is no longer listed and returns 404 by id, that `dispatched_total` restarted from zero for the new run, and that the new run measures a window normally.

It is a live assertion of the measurement infrastructure's run boundary. Write it at Milestone 5, not as part of the Milestone 4 closeout.

## 6. Milestones 6 and 12: the host and node clocks must be compared each run

- The harness stamps `Alert.fired_at`, `IncidentRuntimeContext.now` and `InjectedFault.injected_at` with the **host's** clock (macOS). Kubernetes events, container logs and Prometheus samples carry the **kind node's** clock, which is the Docker Desktop VM's.
- Measured at the final Milestone 4 review (2026-11-19), node minus host was +0.05 to +0.14 s, and that includes `docker exec` latency. That's harmless today.
- But Docker Desktop's VM clock can drift, for example after the host sleeps, and nothing would notice. The alert would then claim an onset the cluster's own timeline contradicts.

**Requirement:**
- **Milestone 6:** before the agent's time-windowed tools (`last_minutes`, the metric `window`) are trusted, the harness measures the host–node clock offset at the start of each run and records it in the run record.
- **Milestone 12:** a run whose offset exceeds a bound is flagged, before the `telemetry_conflict` rule "event timeline contradicts claimed failure onset" (§11.5) compares the two clocks. The bound is decided then, from the measured offsets.

The mechanism is open:
- reading the node's time over decision 0012's host-side `docker exec` would widen that capability's stated use (clean-state inspection only), so it needs its own decision;
- an API-server-side source, such as a response `Date` header, needs none.

## Not carried

The Milestone 4 fix list's code changes (the loadgen run boundary, backend provenance, load cleanup on a lost start, the window invariant, the threshold rename) are recorded in decision 0015's third amendment and decision 0009's amendment, not here.
