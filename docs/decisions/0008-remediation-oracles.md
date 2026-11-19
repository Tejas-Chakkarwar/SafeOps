# 0008 — Remediation lists are positive-only oracles

**Status:** Accepted (2026-11-19)
**Design refs:** §7.3, §9.5, §11.7

## Decision

`valid_remediations` and `harmful_remediations` in a fault spec are evaluator-only test oracles. They never set a label. **The harm checker and the effectiveness verifier are the only sources of truth** for `harmful` and `effective`.

Both lists are positive-only, and the asymmetry is the point:

| Statement | Means |
|---|---|
| An action **listed** in `valid_remediations` | must recover the service when tested |
| An action **not listed** in `valid_remediations` | says nothing about whether it would recover |
| An action **listed** in `harmful_remediations` | must be flagged harmful when tested |
| An action **not listed** in `harmful_remediations` | says nothing about whether it is safe |

**Anything resembling `effective = action in fault.valid_remediations`, or `harmful = action in fault.harmful_remediations`, is the bug this decision exists to prevent.** Using the lists as labels would score the agent against the author's expectations instead of the cluster's behavior, and every result would silently inherit the author's omissions.

## Lists are representative, not exhaustive

- **List order carries no meaning.** There's no "primary" remediation.
- **A fault can be repairable by actions that aren't listed.** Example: `memory-limit-low` declares `patch_deployment_resources`. A `rollback_deployment` would also repair it, because container limits live in the pod template (decision 0007, configuration split), and the fault changes that template. The spec doesn't pretend otherwise. If the agent rolls back, the effectiveness verifier decides, and it will see recovery.

## How the oracles are used

1. **Milestone 11 test, executed, not documented:** for every fault with at least one `valid_remediations` entry, execute at least one representative valid remediation against the injected fault and confirm the service recovers, judged by the effectiveness verifier. A declared remediation that has never been executed is an assumption.
2. **Disagreement is a warning, never an override.** If the harm checker says an action in `harmful_remediations` wasn't harmful, or the effectiveness verifier says an action in `valid_remediations` didn't recover the service, that's a bug in the checker, the ranges, the testbed, or the list. Analysis surfaces it as a warning. It is never resolved by overriding the label.
3. **The lists never cross the information wall** (§6). Agent, features and gate never see them.
