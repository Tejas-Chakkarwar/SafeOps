# 0009 — Workload generator, the effectiveness data path, and the harness identity

**Status:** Accepted (2026-11-19), Milestone 2
**Design refs:** §3, §4, §5, §7.1, §11.1, §11.7

## Invariant: the effectiveness path is independent of Prometheus

`EffectivenessVerifier`, and the steady-state baseline it compares against, get workload measurements **directly from the workload generator**, over a path that doesn't involve Prometheus. Two consumers are kept separate:

| Consumer | Path | Purpose |
|---|---|---|
| harness | direct workload-generator path (below) | evaluator ground truth: steady state, effectiveness |
| agent | workload generator and services → Prometheus scrape → `get_metrics` | agent-facing telemetry |

Prometheus may show workload metrics to the agent (`loadgen_attempts_total`, `loadgen_attempt_duration_seconds`), but no verdict depends on Prometheus.

## Placement

- The generator runs in-cluster as Deployment and Service `workload-generator` in the namespace **`safeops-workload`** (`workload_namespace`).
- It is measurement infrastructure, not part of the system under test. It must not live in `testbed`:
  - `safeops-reader` gets `pods/log` in `testbed` (§4), so it could read the evaluator path;
  - testbed reset and snapshots must never touch it (§11.1).
- `safeops-reader` never gains any access to the workload namespace or its proxy. Milestone 6's RBAC test must assert that.

## Mechanism: the API-server service proxy, `GET /stats` and `PUT /control`

- **Paths:** `GET /api/v1/namespaces/safeops-workload/services/workload-generator/proxy/stats[?window=<id>]`, and `PUT .../proxy/control`. These are sent with `kubectl get --raw` and `kubectl replace --raw`, so there's no port-forward process to manage.
- **Plain Service name:** the path uses the bare name (`workload-generator`), not `http:workload-generator:8080`, so the RBAC `resourceName` matches exactly.
  - That requires the Service's single port to be **unnamed**.
  - Found live on 2026-11-19: with the port named `http`, the bare-name proxy returned `ServiceUnavailable: no endpoints available` even though the pod was ready, while `workload-generator:http` worked.
  - `tests/test_testbed_manifests.py` now asserts one unnamed port.
- **Chunked request bodies:** kubectl sends the PUT to the API server with `Content-Length`, but the API server's service proxy re-streams it to the pod with `Transfer-Encoding: chunked`. The testbed HTTP handler (`testbed/app/common.py`) therefore reads both, with a 1 MiB cap. Found live on 2026-11-19, when the first `PUT /control` arrived with an empty body and was rejected with 400.
- **Checking RBAC with can-i:** `kubectl auth can-i` takes the subresource as `--subresource=proxy`. It doesn't parse `services/<name>/proxy`, and silently answers "no" for that form. Both the bootstrap health check and the live test use `--subresource`.
- **RBAC:** Role `experiment-controller-workload-proxy` in `safeops-workload`, with `services/proxy`, `resourceNames: ["workload-generator"]`, `verbs: ["get", "update"]`. PUT maps to `update`, which sidesteps any question about `create` plus `resourceNames` on subresources.
- **Settled by tests, not doctrine.** `tests/cluster/test_live.py` (`make cluster-test`) proves against the live API server that `experiment-controller`:
  - can proxy GET and PUT to `workload-generator`;
  - is **denied** for a second Service that really exists in the same namespace (a decoy the test creates and deletes with the admin credential);
  - is denied for Prometheus, testbed Services, Secrets and pods.
  - `cluster-up` also runs `kubectl auth can-i` checks for the same rules.
- **The wrapper has its own capability check.** The object-mutation guard doesn't cover the proxy path, so `WorkloadProxy` checks namespace, Service name, and operation ∈ {`stats`, `control`}, and refuses odd query strings. Underneath it, a runtime-phase `Kubectl.raw` refuses any path outside the workload generator's proxy prefix. A bug in the wrapper can't reach a different Service.

## Generator API

| Call | Effect |
|---|---|
| `PUT /control {"action":"start","rate_rps","deadline_s","path"}` | open-loop load; attempts are dispatched on schedule, never paced by responses |
| `PUT /control {"action":"measure","attempts"}` → `window_id` | the next `attempts` dispatched attempts form a window |
| `PUT /control {"action":"stop"}` | stop; a window still collecting becomes a measurement failure |
| `GET /stats?window=<id>` | window summary |

## Steady state: a fixed denominator (`config/workload.yaml`)

- **Load:** 10 req/s, `GET /checkout` through `gateway-svc`.
- **Warmup:** 15 s of load before the window opens. Accepted at the Milestone 2 review. Revisit only before the pilot freeze, and only if fault behavior gives a reason.
- **The window is exactly 300 scheduled attempts**, which at 10 req/s means 30 s of scheduling. The denominator is always 300.
- **An attempt succeeds only with HTTP 200 inside its 1-second deadline, measured from dispatch.** A timeout, an error, a non-200, a late 200, and no response at all are all failed attempts.
- **Where the window ends:** after the 300th attempt is dispatched, outstanding attempts get until their own deadline. The window then closes at `last_dispatch + 1 s + 0.5 s settle`, and is computed over all 300.
  - Outcomes are classified from the recorded response time, so the settle margin only covers the gap between a response arriving and its outcome being recorded. It never extends the deadline.
  - Stopping at exactly t = 30 s would count the last requests inconsistently.
- **Baseline passes at ≥ 297 of 300.**
- **Measurement failure:** if the generator can't schedule all 300 (stopped, or lost), or the window doesn't close within 60 s, that's a measurement failure, never a smaller denominator.
- **A failed baseline is a pre-run failure.** §7.1's discard list doesn't name it, but §11.1 discards pre-run failures: discard the run and count it. The runner hook `_require_steady_state` raises `SteadyStateNotReached` or `SteadyStateMeasurementFailed`; recording the discard is Milestone 5.
- **Where the logic lives:** the accounting is in `testbed/app/window.py`, which is stdlib-only, so SafeOps tests drive it with fabricated attempts. The pass/fail rule is `check_steady_state`, shared by the runner and `safeops steady-state`.
- **Recorded, not gated:** p95 latency of successful attempts, and the maximum dispatch lag.

## Harness identity: `experiment-controller`

- **It lives in `safeops-system`** (`system_namespace`), so it survives Milestone 3 rebuilding or quarantining `testbed`.
- **Credentials by phase:**
  - Bootstrap uses the admin kubeconfig that `kind` wrote (`.safeops/kubeconfig`).
  - Every runtime command uses `experiment-controller`'s credential (`.safeops/experiment-controller.kubeconfig`).
  - **Runtime never falls back to the admin kubeconfig.** A missing, invalid or expired credential fails loudly (`CredentialError`; CLI exit code 5). A fallback would silently undo the separation of phases by credential.
- **The credential is a time-limited TokenRequest ServiceAccount token.** It isn't a "bound token": no `boundObjectRef` is supplied.
  - Bootstrap requests `harness_session_s` and then **reads the issued expiry back from the token** instead of trusting the request; the API server can cap durations with `--service-account-max-token-expiration`.
  - **The lifetime is 7 days (604800 s)**, raised from 24 h at the Milestone 2 closeout (2026-11-19). Milestone 3 is where `experiment-controller` gains testbed lifecycle privileges and starts to matter, so an expiry mid-session would be confusing, not just annoying. A development cluster session is now up to 7 days.
  - If the issued lifetime is shorter than requested, bootstrap fails clearly and writes no credential.
  - The lifetime and expiry, never the token, go into `.safeops/experiment-controller.token.json`.
- **No refresh machinery at Milestone 2.** Revisit before Milestone 14, once campaign duration has been measured.
- **Milestone 2 permissions:** only the proxy rule above. Its `testbed` rights arrive at Milestone 3, with reset.

## CLI

`safeops steady-state` (also `make steady-state`) starts the load, measures one window, stops the load (always, in `finally`), and prints the summary. Exit codes: 0 means the baseline passed; 4 means no valid baseline; 5 means a guard refusal, a missing or expired credential, or a failed cluster command.

## Amendment, 2026-11-19: Milestone 2 adversarial review fixes

**The harness doesn't trust the generator's summary (W1–W4).**
- A `complete` window counts as measured only if all of these hold:
  - `size` equals `window_attempts`;
  - `scheduled` equals `size`;
  - `succeeded` and `failed` are integers (not bools);
  - `0 ≤ succeeded ≤ size`;
  - `succeeded + failed = size`;
  - the window's `deadline_s` equals `attempt_deadline_s`.
- Anything else is a **measurement failure** ("inconsistent summary"), never a baseline.
- Before the fix, the harness accepted 301 successes out of 300, counts that didn't add up, and a window measured with a 5-second deadline.

**A lost generator or window is a measurement failure (W6).** A generator restart mid-window, or any other `CommandError` while measuring, now gives `SteadyStateMeasurementFailed`, not a generic command error. It's still a discardable pre-run failure, but now in the right bucket for discard reporting.

**A busy generator fails loudly and is never stopped automatically (W5).**
- `start` answering 409 raises `WorkloadGeneratorBusy`, a subclass of `SteadyStateMeasurementFailed`, telling you to run `safeops workload-stop`.
- Auto-stopping could clobber a concurrent run that shares the generator.
- `safeops workload-stop` is the manual recovery after a run that died without stopping it.

**The body parser is testable.** Chunked and `Content-Length` request-body parsing moved into the stdlib-only `testbed/app/httpbody.py`, which is copied into both images through the base stage. `tests/test_m2_review.py` now tests it offline: chunk extensions, trailers, truncation, missing CRLF, the size cap, and bad lengths.

## Amendment, 2026-11-19: second review

- **C2: the evaluator path is harness-only in code, not just by default.** `WorkloadProxy` refuses any `Kubectl` that isn't runtime-phase, so it can't run with the admin credential.
- **M1: a closed window is frozen.** `Window.summary` computes a complete window exactly once and returns that frozen result forever after. The generator also freezes every window the moment it closes, in a 100 ms ticker, so the number doesn't depend on when the harness reads it. Before this, a response that arrived in time but was recorded after close changed a closed window's count between two reads, from 2 to 3 in the probe.
- **M2: runs can't bleed into each other.** Every `start` bumps a run counter. A scheduler thread admits an attempt only if, under the lock, its stop flag is still clear and its counter is still current. So a stale thread from the previous run can't put one attempt into the next run's window.
- **M3: the system under test can't reach its evaluator.**
  - NetworkPolicy `workload-generator-ingress` (in `safeops-workload`) admits ingress to the generator only from `safeops-monitoring`, for the Prometheus scrape, on port 8080.
  - The harness's path is unaffected. It goes through the API-server proxy, the API server runs on the node, and kindnet admits node-originated traffic.
  - Verified live on 2026-11-19:
    - with the policy, a `testbed` pod's connection was blocked;
    - the harness proxy kept working, with and without a node-IP rule, so no node-IP rule is needed;
    - the Prometheus target stayed up.
  - `tests/cluster/test_live.py::test_testbed_pods_cannot_reach_the_evaluator` keeps checking this, so a CNI that behaves differently fails loudly.
