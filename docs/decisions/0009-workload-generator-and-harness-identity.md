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
- **Warmup:** 15 s of load before the window opens. Proposed on 2026-11-19 and not objected to. Flag it at review if that's wrong.
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
  - Bootstrap requests `harness_session_s` (86400 s, 24 h) and then **reads the issued expiry back from the token** instead of trusting the request; the API server can cap durations with `--service-account-max-token-expiration`.
  - If the issued lifetime is shorter than requested, bootstrap fails clearly and writes no credential.
  - The lifetime and expiry, never the token, go into `.safeops/experiment-controller.token.json`.
- **No refresh machinery at Milestone 2.** Revisit before Milestone 14, once campaign duration has been measured.
- **Milestone 2 permissions:** only the proxy rule above. Its `testbed` rights arrive at Milestone 3, with reset.

## CLI

`safeops steady-state` (also `make steady-state`) starts the load, measures one window, stops the load (always, in `finally`), and prints the summary. Exit codes: 0 means the baseline passed; 4 means no valid baseline; 5 means a guard refusal, a missing or expired credential, or a failed cluster command.
