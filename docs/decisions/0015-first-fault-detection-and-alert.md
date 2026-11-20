# 0015 — First fault: injection, detection on the harness path, the alert, post-fault settledness

**Status:** Accepted (2026-11-19), Milestone 4
**Design refs:** §5, §6, §9.5, §11.2, §11.8 (`alert`, `fault_verified`), §14 (Milestone 4); decisions 0003, 0004, 0007, 0008, 0009, 0013, 0014, 0016

## `bad-deployment-config`

- **Mechanism.** The fault replaces one plain pod-template env value of `orders` (`faults/bad-deployment-config.yaml`). It never touches a ConfigMap, so a rollback genuinely repairs it (decision 0007). Four variants, selected by seed (decision 0014):

  | Variant | Env | Value | orders gets |
  |---|---|---|---|
  | 0 | `PAYMENT_URL` | `http://payments-svc:8080` | DNS failure |
  | 1 | `PAYMENT_URL` | `http://inventory-svc:8080` | 404 from `/charge` |
  | 2 | `INVENTORY_URL` | `http://inventroy-svc:8080` | DNS failure |
  | 3 | `INVENTORY_URL` | `http://inventory-svc:8080/v2` | 404 from `/v2/reserve` |

  Each one makes orders answer every `POST /orders` with 502.
- **Backend `kubernetes-api`, action `set_container_env`** (`safeops/faults/injector.py`).
  - It reads the Deployment, changes the one value, and PUTs it back through the guarded `Kubectl.replace`. That's runtime phase, `experiment-controller`, testbed only.
  - `resourceVersion` is kept, so a concurrent change makes the PUT fail rather than be overwritten.
  - Injection **refuses** a change that would be a no-op, which would be silent non-injection at the source, and refuses a value that is missing or comes from `valueFrom`.
- **No new RBAC.** The Milestone 3 testbed Role already grants `update` on Deployments. Chaos Mesh create rights arrive with the first Chaos-backed fault (Milestone 11), scoped then.
- **No new node residue.** This backend changes only a projected Kubernetes object. The post-run reset recreates the app tier (0013), and verify's desired-state diff would catch any survivor. 0012's extension point is unused for now.
- `clear()` restores the replaced value, as a new revision. The run sequence never relies on it: reset is the cleanup.

## Detection: measured on the harness path, never Prometheus

- **`verify_fault_present` measures fixed-denominator windows on the running workload,** through the same generator, proxy and cross-checks as the steady state (`WorkloadGenerator.measure_window`). Decision 0009's invariant holds: no verdict depends on Prometheus, and the harness gains no Prometheus access.
- **`http_5xx_rate`** in a symptom expression means `server_error_responses / attempts` over one window of `window_s × rate_rps` scheduled attempts, for example 30 s × 10 = **300**, the steady-state denominator.
- **A 5xx counts only if it was answered inside the 1 s deadline.** A late response is already a timeout, so the buckets stay disjoint: succeeded + error responses + timed out or no response = size.
  - `testbed/app/window.py` gained `server_error_responses`. That changed the loadgen digest and left the testbed digest unchanged, as 0007 requires.
  - The harness refuses a summary whose 5xx count is missing, isn't an integer, or exceeds `failed` or `error_responses`.
- **A window that can't be measured raises `FaultVerificationFailed`** (infrastructure), which is distinct from a symptom that was measured and absent.

## The `for 2m` question

The design's example expression is `http_5xx_rate > 0.2 for 2m` (§9.5). If verification had to observe two minutes of errors before the agent starts, every run would get two minutes longer: about 10 hours across a 300-run Phase A campaign.

**Decision: one expression, evaluated by the harness exactly as written, and shortened. The alert's text is not separated from the harness check.**

- **The expression is `http_5xx_rate > 0.2 over 30s`.** The threshold stays at the design's 0.2 (calibrated in 0016). The window is 30 s.
- **`over`, not `for`.** The harness computes the ratio over one window. That is not Prometheus's `for`, which means the condition held at every evaluation for the whole duration. A window that is 33 % errors because the last 10 s failed satisfies `> 0.2 over 30s` but not `> 0.2 for 30s`. Writing `for` would claim something nobody checked. The grammar (`<metric> > <0.x> over <N>s`) refuses `for`.
- **30 s, because it is the steady-state window.** 300 scheduled attempts, the same denominator, the same 1/300 resolution, and the same cross-checks. The fault clears the threshold by about 0.8 (0016), so a shorter window would also detect it. But a second window size is a second set of semantics, for a saving of about 20 s per run.
- **Cost, measured:** see "Live acceptance" below.
- **Truthful by construction.**
  - The alert's `symptom` is the expression, verbatim, as §11.8 shows it.
  - It is built **only** from a verification in which the harness measured that expression holding. `build_alert` refuses a verification where it didn't.
  - `fired_at` is the time the holding window was read, not the injection time.
  - So everything the agent reads in the alert was true of the cluster at `fired_at`.
- **`verify_fault_present` timeout: 95 s, room for three 30 s windows.** The first window usually overlaps the rollout. If a whole timeout passes with no window holding, that's silent non-injection, which costs about 95 s, only on a run that is discarded anyway.

### What the measurement does and doesn't attribute

- The harness measures 5xx at the **user-facing entry point**: `/checkout` through gateway, a path that traverses `orders`. It does not, on its own, attribute those 5xx to `target_service`.
- `target_service` is the symptom's declared routing label, the way a real alert rule's labels are declared. The fault spec author is responsible for it.
- **For `bad-deployment-config` the attribution is checked live, not assumed.** `tests/cluster/test_fault_live.py` reads orders' own `/metrics` (test-only, admin credential, never a harness path) and requires ≥ 95 % of `POST /orders` to be answered with 5xx while the fault is active.
- **Open for Milestone 11:** a fault whose alert target isn't the origin of the entry-point 5xx needs its own attribution check, or a symptom the harness measures at that target. Don't quietly reuse this check for such a fault.

## The alert and the information wall

- **`Alert` and `IncidentRuntimeContext` live in `safeops/context.py`.** That is a neutral module, and it is now also in the information-wall test's protected set, so it can never import `safeops.faults` (decision 0004).
- **The alert generator never receives the spec.** Its only input is `FaultVerification(present, symptom, observations, reason)`, where:
  - `symptom` is the `Symptom` parsed from the spec's `symptoms:` field;
  - `observations` are the harness's measured windows.

  It holds no reference to the spec or to the `InjectedFault`. `build_alert` raises `TypeError` for anything else, including an `InjectedFault` or a `FaultSpec`.
- **The Alert takes:**
  - `alert_name`, `target_service` and `symptom` from the verified `Symptom`;
  - `namespace` from `config/cluster.yaml`, never the spec;
  - `fired_at` from the observation.
- **Tests** (`tests/test_m4_faults.py`):
  - a **sentinel** spec whose fault id, affected service (deliberately ≠ target), remediation lists and injection values are all disjoint from its symptom. No such value, whole or as a component, appears in any Alert field or in the Alert's repr.
  - a **differential** test: changing every non-symptom field leaves the Alert identical, while changing the symptom changes it.
  - a structural test of the generator's input type.
- **Mutation check, done:** making the injector copy `affected_service` into the verified symptom, and separately making the generator ignore the symptom, each failed the sentinel and differential tests.
- **`IncidentRuntimeContext(alert, topology, now)`** is built by the runner's `_runtime_context` hook. It is frozen and type-checked, and `now` is timezone-aware.
- **Spec schema** (`safeops/faults/spec.py`): §9.5's fields plus an evaluator-only `injection:` block. Keys are exact. Remediation oracles must read `<action_type>:<service>[:cond]` over §9.1's action names, and they stay positive-only (0008). For now **exactly one symptom per fault** (one alert), until a fault genuinely needs more. `definition_sha256` (of the file's bytes) is kept for Milestone 5's `fault_definition_hash`.

## Post-fault settledness (0013's Milestone 4 requirement)

- **`EnvironmentManager.capture_settled_snapshot()`** replaces the bare post-fault `capture_snapshot()` in the run sequence. It polls the canonical projection every **5 s** and is settled once **3 consecutive polls are identical**, which spans two intervals. The **timeout is 60 s**.
  - Identical means an empty `snapshot.diff` and an equal canonical digest.
  - 5 s is the testbed's readiness probe period, so a pod's readiness can change at most once between polls.
- **It returns `SettledSnapshot(snapshot, settled, polls, elapsed_s, last_changes)`.**
  - When it isn't settled, the snapshot is the last poll, and `last_changes` says what was still moving. The flag goes to the record writer (Milestone 5), so an unsettled baseline is never silently treated as clean.
  - The harm checker receives `.snapshot`, as §11.7's signature says.
- **It is observed, not declared.** There's no per-fault list, and settled ≠ healthy. Zero ready endpoints that stay at zero is settled (tested).
- **Milestone 3's health stabilization is untouched.** Reset and verify still wait for health; settledness never calls it (tested).
- The projection and its version (2) are unchanged.

## Runner, CLI, discards

- **The order is now:** `inject` → `verify_fault_present` → `_require_fault_present` (raises `FaultNotPresent` for silent non-injection) → `capture_settled_snapshot` → `build_alert(verification)` → `_runtime_context`. The first stub is now `Agent.investigate` (Milestone 9).
- `finalize` additionally receives `fault` and `fault_verification`.
- **`safeops run` exit codes:**
  - **7 is new:** discarded before the agent, for silent non-injection, a failed injection, an unmeasurable detection window, or a dirty pre-run environment.
  - 4: no steady-state baseline.
  - 5: guard, credential or cluster error.
  - 2: an invalid unit or an unknown fault.
- **Still Milestone 5, unchanged (0004's note):** recording discards, and cleaning up after a mid-run failure. A run that stops at a discard or at the agent stub leaves the fault and the load in place. The next run's pre-run reset removes the fault, and `safeops workload-stop` stops the generator.

## Live acceptance (2026-11-19, `tests/cluster/test_fault_live.py`, 7 passed)

- **Injection reliably produces measurable errors.** Five injections across all four variants each passed the following, and each post-run reset verified clean. The numbers are in decision 0016.
  - The steady state was 300/300.
  - `verify_fault_present` held on the **first** window, at 0.69–0.72.
  - The baseline settled in 3 polls (10.6–10.8 s), and differed from the pre-fault snapshot **only** in `Deployment/orders`.
  - The post-rollout magnitude was 1.0, and orders' own `/metrics` showed 100 % 5xx on `POST /orders`.
- **Silent non-injection is caught.** A fabricated fault that rolls out a real new gateway revision but breaks nothing measured 0.0 in three windows. It was reported absent after 97 s, and `_require_fault_present` raised `FaultNotPresent`.
- **The alert is built from symptoms only.** The real runner ran end to end, from reset to the agent stub (Milestone 9), and produced exactly `{"alert_name": "HighErrorRate", "namespace": "testbed", "target_service": "orders", "symptom": "http_5xx_rate > 0.2 over 30s", "fired_at": …}`. None of the fault's id, remediation lists or injection values appeared in it, and the agent received an `IncidentRuntimeContext` carrying exactly that alert.
- **Measured cost per run:**
  - about 32.4 s from injection to a verified symptom (one window plus polling);
  - about 10.6 s of settling;
  - about 43 s in total.

  Across a 300-run campaign that's about 3.6 h, against about 10 h or more for a literal two-minute `for` (two minutes of observation plus the same settling).
