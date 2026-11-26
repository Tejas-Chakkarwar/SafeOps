"""Loud placeholder transports (decision 0025, M8B-1 amendment).

Milestone 8B-1 wired these in build_runner until Milestone 8B-2 supplied the concrete
safeops-executor writer and safeops-reader reader (safeops/executor/kubernetes.py), which
build_runner now uses. They are no longer production wiring; offline tests keep them as a
transport that is loud by construction. They hold no credential and no client, so no
Kubernetes mutation (or read) is possible through them: each method is a Milestone 8 stub,
raising NotImplementedStage before anything is sent.
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
