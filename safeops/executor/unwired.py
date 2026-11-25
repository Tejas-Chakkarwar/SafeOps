"""The executor's transitional production transports (decision 0025, M8B-1 amendment).

Milestone 8B-1 builds the executor's semantics offline; Milestone 8B-2 supplies the
concrete safeops-executor writer and safeops-reader adapters. Until then, build_runner
wires these. They hold no credential and no client, so no Kubernetes mutation (or read) is
possible through them, and reaching one is loud: each method is a Milestone 8 stub, raising
NotImplementedStage before anything is sent. A run that gets that far is recorded as a
not_implemented discard at stage execute, never as an outcome.
"""

from __future__ import annotations

from safeops.stubs import stub


class UnwiredWriter:
    @stub(milestone=8)
    def preflight(self, requests):
        """Milestone 8B-2: the safeops-executor writer adapter."""

    @stub(milestone=8)
    def write(self, request):
        """Milestone 8B-2: the safeops-executor writer adapter."""


class UnwiredReader:
    @stub(milestone=8)
    def get(self, kind, name, namespace, timeout_s):
        """Milestone 8B-2: the executor's safeops-reader adapter."""

    @stub(milestone=8)
    def list(self, kind, namespace, timeout_s):
        """Milestone 8B-2: the executor's safeops-reader adapter."""
