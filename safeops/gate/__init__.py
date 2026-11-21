"""Gate protocol and strategies (design §10.3, §10.7). The gate takes a FeatureVector only.

Information wall (§6; decision 0020): this package never reaches evaluator or harness
state: nothing under safeops.faults, safeops.experiment, safeops.verify,
safeops.environment, safeops.bootstrap or safeops.analysis, directly, transitively or
lazily. It consumes only neutral modules: safeops.context (IncidentRuntimeContext),
safeops.domain (shared domain types), safeops.enums and safeops.topology. Enforced by
tests/test_information_wall.py.
"""
