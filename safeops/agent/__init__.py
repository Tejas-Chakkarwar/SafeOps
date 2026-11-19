"""Agent loop, prompts, trajectory, proposal parser (design §11.4).

Information wall (§6): this package receives IncidentRuntimeContext only and must
never import from safeops.faults (enforced by tests/test_information_wall.py).
"""
