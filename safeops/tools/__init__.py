"""Read-only diagnostic tools with bounded outputs (design §11.3). Milestone 6.

The agent's senses, so protected like the agent itself.
Information wall (§6; decisions 0020, 0021): this package never reaches evaluator or
harness state: nothing under safeops.faults, safeops.experiment, safeops.verify,
safeops.environment, safeops.bootstrap or safeops.analysis, directly, transitively or
lazily. It may depend on neutral runtime modules, for example safeops.context
(IncidentRuntimeContext), safeops.domain (shared domain types), safeops.enums and
safeops.topology; that list is illustrative, the boundary is what's enforced, by
tests/test_information_wall.py.
"""
