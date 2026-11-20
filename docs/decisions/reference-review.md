# Reference review notes (Appendix A)

Short, targeted passes, one per milestone that needs them. What we took, what we didn't, and why. Nothing here is code: no reference repository is imported, installed or copied.

## Milestone 4: fault spec schema and injector (2026-11-19)

Scope, per CLAUDE.md: ITBench's SRE scenarios and the AIOpsLab fault library, as a short pass. At the pinned ITBench commit (`c0309d4e`) there is no `scenarios/sre/`. The SRE scenarios live in `archive/scenarios/<n>/` and `library/faults/`, so those are what was read.

### ITBench

- **The injection is kept apart from the ground truth.** Each scenario has `scenario.yaml`, which says what is injected, and `groundtruth_v1.yaml`, which lists the expected alerts, the fault entities, the propagation chain and aliases.
  - **Taken, structurally:** SafeOps keeps one spec file per fault, as §9.5 says. But the loader parses `symptoms:` into its own type (`Symptom`). Only a `FaultVerification` holding that symptom reaches the alert generator, never the spec (decision 0015). The separation is enforced in code rather than by splitting files.
- **Alerts are named for symptoms, not faults.** Examples are `HighRequestErrorRate`, `KubePodNotReady` and `KubePodCrashLooping`, and they fire from real alert rules. Many different faults share one alert name. That matches §6: the alert names the symptom (`HighErrorRate`) and never identifies the fault.
- **ITBench retired its "modified environment variable" fault (v2.0.0)**, because non-generic faults are "prone to working in specific versions of applications and make it difficult to determine outcomes".
  - That's the same mechanism as `bad-deployment-config`. The risk doesn't transfer: SafeOps owns its testbed, the image is pinned by digest, and every run proves the symptom with `verify_fault_present` before the agent starts.
  - The lesson still applies, though: **a fault's effect depends on the application, so it must be verified every run, never assumed.** That's decision 0016.

### AIOpsLab

- **Each problem defines `start_workload`, `inject_fault`, `recover_fault` and `eval`.** The fault library (`generators/fault/inject_*.py`) applies faults with kubectl, Helm and Chaos Mesh. Misconfiguration faults, for example, run a script inside a pod.
- **There is no check, before the agent starts, that the fault produced a symptom.** The workload (`wrk` at a fixed rate) isn't measured either. A silent non-injection would be scored against the agent.
  - SafeOps' `verify_fault_present`, and the discard on silent non-injection (§11.2), exist precisely to close that gap.
- **Not taken:** injection through `kubectl exec` into pods. SafeOps' harness has no `pods/exec` (decision 0013), and config faults go through the pod template, so a rollback can repair them (decision 0007).
