# 0016 — Fault magnitude is calibrated against the measurement thresholds

**Status:** Accepted (2026-11-19), Milestone 4. A rule that holds for every fault in the catalog. Until now it was implicit; decision 0003's F4 finding was its first instance.
**Design refs:** §5 (`verify_fault_present`), §11.2, §11.7 (effective), §14 (Milestones 4 and 11); decisions 0003 (F4, consequence 3), 0009 (steady state), 0015 (detection)

## The rule

A fault's magnitude is never chosen on its own. It is set, and then measured, against the three thresholds it has to sit between:

```
baseline noise ceiling  <  symptom threshold  <  fault magnitude
   (steady state passes)     (the alert holds)     (what the fault does)
```

Each gap needs a clear margin:

1. **The threshold must sit well above what a healthy testbed can produce.** Otherwise a healthy run can "verify" a fault that isn't there, and the agent investigates noise.
2. **The magnitude must sit well above the threshold, for every variant and every seed.** Otherwise injection is flaky, runs are discarded at random, and the discard rate depends on luck, not on the fault.
3. **The fault must break the steady-state criterion, and its valid remediation must restore it**, or `effective` can't distinguish a fix from no fix (Milestones 7 and 11).

**`verify_fault_present` is the per-run proof.** The calibration is the design argument for why it will pass; the check itself is what makes each run's claim true. A fault that injects cleanly but produces no measurable symptom is decision 0003's F4 failure mode: CPU stress against a container with no CPU limit produced nothing. It's caught as silent non-injection and discarded, never scored (§11.2).

## `bad-deployment-config`

The measurement criteria, from decision 0009:
- 10 req/s;
- success means HTTP 200 within 1 s;
- windows of 300 scheduled attempts;
- the baseline passes at ≥ 297.

| Quantity | Value | Reasoning |
|---|---|---|
| Baseline noise ceiling | **≤ 0.01** 5xx ratio | A passing steady state allows at most 3 failed attempts in 300, of any kind, so at most 3 can be 5xx. |
| Symptom threshold | **> 0.2** (more than 60 of 300) | The design's value (§9.5). It is 20× the ceiling: a healthy testbed would need 61 server errors in a window it otherwise passes, which can't happen. |
| Expected magnitude | **≈ 1.0** after the rollout | Every variant breaks one upstream URL that every `POST /orders` uses. orders answers 502, and gateway propagates 502. Each failure is fast (DNS failure or 404, well inside 300 ms), so it shows as a 5xx, not a timeout. |
| Measured magnitude | see "Live calibration" below | — |
| Effect on steady state | ≈ 0 of 300 succeed | It breaks the criterion completely. A rollback to revision 1 restores the healthy template; Milestone 11 executes that (decision 0008). |

**The window that catches the rollout.** Detection starts right after injection, while the new revision is rolling out. That first window mixes healthy and failing requests. With 2 replicas, maxSurge 1 and maxUnavailable 0, the new pods take traffic within seconds. Even if only a third of the window falls after the switch, the ratio is ≈ 0.33 > 0.2. And if a window misses, the next one sees the full fault (decision 0015 allows three windows).

## Live calibration

Measured on 2026-11-19 by `tests/cluster/test_fault_live.py`. There were five injections; seeds 0, 1, 5 and 7 cover all four variants, and seed 2 repeats variant 3.

| Seed | Variant | Steady state | First detection window (spans the rollout) | After the rollout | orders' own `POST /orders` 5xx |
|---|---|---|---|---|---|
| 0 | 3 | 300/300 | 0.717 | 1.000 | 1.000 |
| 1 | 0 | 300/300 | 0.713 | 1.000 | 1.000 |
| 5 | 2 | 300/300 | 0.717 | 1.000 | 1.000 |
| 7 | 1 | 300/300 | 0.707 | 1.000 | 1.000 |
| 2 | 3 | 300/300 | 0.693 | 1.000 | 1.000 |

- **The healthy baseline sat at 0 of 300 failures** every time, well under the 0.01 ceiling.
- **The first window held every time, at ≈ 0.70.** That's 3.5× the threshold, even though about 30 % of it ran before the new pods took over.
- **The fully rolled-out magnitude was exactly 1.0.**
- **The variants are indistinguishable in magnitude**, which is what decision 0014 requires.
- **A fault built to show no symptom** (a gateway `LOG_LEVEL` change that is a real new revision) measured 0.0 in all three windows and was reported absent. That is the F4 mode, caught.

## For the rest of the catalog (Milestone 11)

Each fault's decision entry records, before its Milestone 11 run:
- its noise ceiling, threshold and expected magnitude, with the reasoning above;
- the magnitude **measured** across its 5 seeds;
- why its symptom is observable on the harness's own measurement path (decision 0015).

Known constraints that follow from this rule:
- **Latency faults** are sized by measured end-to-end latency, not by the configured per-packet delay (0003, consequence 3). A delay compounds per round trip, and the 1 s deadline is what counts.
- **Resource-pressure faults** need real limits on the target (0003 F4), which the testbed has.
- **`memory-limit-low`** has to lower request and limit together (0011). It must still OOM-kill reliably: the limit has to sit below the 64 MiB `CACHE_MB` working set plus the runtime (decision 0007).
- **Every variant of a fault must have a comparable magnitude.** The seed changes how a fault is realized, never whether it is detectable (0014).
