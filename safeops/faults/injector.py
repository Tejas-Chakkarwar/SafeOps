"""Fault injector (design §11.2; decisions 0014, 0015).

Injects one fault, deterministically from (fault_id, fault_seed), then proves its symptom
is observable before the agent starts. The proof is a measurement on the harness's own
path (the workload generator, never Prometheus; decision 0009): a fault that injects
cleanly but shows no symptom is silent non-injection, and the run is discarded (§11.2).

Runtime only: every Kubernetes call uses experiment-controller through the guarded
Kubectl wrapper, inside the testbed namespace. Never the admin credential.

Import policy (decision 0004): reachable from RUN_SEQUENCE, so safeops.cluster and the
workload module are imported inside the methods that need them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from safeops.faults.spec import (
    FAULTS_DIR,
    FaultSpec,
    FaultSpecError,
    Injection,
    Symptom,
    Variant,
    load_fault_spec,
)

# How long verify_fault_present keeps measuring windows before calling the fault silent.
# Room for three 30 s windows: the rollout overlaps the first (decision 0015).
VERIFY_TIMEOUT_S = 95.0


class FaultInjectionError(Exception):
    """The fault couldn't be injected as specified (target missing, or the change would be
    a no-op). A pre-agent failure: the run is discarded, never scored."""


class FaultVerificationFailed(Exception):
    """The symptom couldn't be *measured* (generator lost, inconsistent window). An
    infrastructure failure, distinct from a symptom that was measured and absent."""


class FaultNotPresent(Exception):
    """Silent non-injection: the symptom was measured and did not hold (§11.2). Raised by
    the runner before the agent starts; the run is discarded and counted."""


@dataclass(frozen=True)
class InjectedFault:
    """Evaluator ground truth about what was injected. Never crosses the wall (§6)."""

    spec: FaultSpec
    fault_seed: int
    variant_index: int
    variant: Variant
    previous_value: str
    injected_at: datetime
    backend: dict = field(default_factory=lambda: {"name": "kubernetes-api", "version": None})


@dataclass(frozen=True)
class SymptomObservation:
    """One detection window: the measured value of the symptom's metric."""

    window_id: str | None
    attempts: int
    server_error_responses: int
    value: float  # http_5xx_rate over the window
    held: bool
    observed_at: datetime  # when the harness read the closed window


@dataclass(frozen=True)
class FaultVerification:
    """What verify_fault_present established. It holds the declared symptom and the
    measurements, and deliberately no reference to the fault spec, so it is the only
    thing the alert generator ever receives (decision 0015)."""

    present: bool
    symptom: Symptom
    observations: tuple[SymptomObservation, ...]
    reason: str | None = None

    @property
    def held_observation(self) -> SymptomObservation | None:
        return next((o for o in self.observations if o.held), None)


class SetContainerEnv:
    """Kubernetes-API backend: set one plain env value in a Deployment's pod template.

    The value must already exist as a plain ``value`` (not ``valueFrom``): those are the
    values a rollback repairs (decision 0007). The change creates a new revision.
    """

    name = "kubernetes-api"

    def __init__(self, kubectl, namespace: str) -> None:
        self._k = kubectl
        self._ns = namespace

    def _env_entry(self, deploy: dict, injection: Injection, env: str) -> dict:
        containers = (
            deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        )
        container = next((c for c in containers if c.get("name") == injection.container), None)
        if container is None:
            raise FaultInjectionError(
                f"Deployment/{injection.name} has no container {injection.container!r}"
            )
        matches = [e for e in container.get("env") or [] if e.get("name") == env]
        if len(matches) > 1:
            # Which duplicate the container sees isn't ours to guess: changing the "wrong"
            # one would report an injection that never happened (review finding 5).
            raise FaultInjectionError(
                f"{injection.name}/{injection.container} defines {env!r} {len(matches)} times"
            )
        entry = matches[0] if matches else None
        if entry is None or "valueFrom" in entry or not isinstance(entry.get("value"), str):
            raise FaultInjectionError(
                f"{injection.name}/{injection.container} has no plain env value {env!r} to change"
            )
        return entry

    def _set(self, injection: Injection, env: str, value: str, expect: str | None) -> str:
        deploy = self._k.get_json("deployment", injection.name, "-n", self._ns)
        entry = self._env_entry(deploy, injection, env)
        previous = entry["value"]
        if expect is not None and previous != expect:
            raise FaultInjectionError(
                f"{injection.name}/{injection.container} {env} changed since injection"
            )
        if previous == value:
            raise FaultInjectionError(
                f"setting {env} on {injection.name} would change nothing (silent non-injection)"
            )
        entry["value"] = value
        deploy.pop("status", None)
        deploy.get("metadata", {}).pop("managedFields", None)
        # resourceVersion stays: the PUT fails rather than overwrite a concurrent change.
        self._k.replace([deploy])
        return previous

    def apply(self, injection: Injection, variant: Variant) -> str:
        """Apply the variant; return the value it replaced."""
        return self._set(injection, variant.env, variant.value, None)

    def remove(self, injection: Injection, variant: Variant, previous_value: str) -> None:
        self._set(injection, variant.env, previous_value, variant.value)


class FaultInjector:
    """Construction has no side effects: the cluster and workload are reached lazily."""

    def __init__(
        self,
        kubectl=None,
        workload=None,
        faults_dir: Path = FAULTS_DIR,
        topology=None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        verify_timeout_s: float = VERIFY_TIMEOUT_S,
    ) -> None:
        self._kubectl = kubectl
        self._workload = workload
        self._faults_dir = faults_dir
        self._topology = topology
        self._clock = clock
        self._wall_clock = wall_clock
        self.verify_timeout_s = verify_timeout_s
        self._namespace: str | None = None

    def _k(self):
        if self._kubectl is None:
            from safeops.cluster import Kubectl, Phase, load_cluster_config

            self._kubectl = Kubectl(load_cluster_config(), Phase.RUNTIME)
        return self._kubectl

    def _ns(self) -> str:
        if self._namespace is None:
            k = self._k()
            if getattr(k, "cfg", None) is not None:
                self._namespace = k.cfg.namespace
            else:
                from safeops.cluster import load_cluster_config

                self._namespace = load_cluster_config().namespace
        return self._namespace

    def _load_workload(self):
        if self._workload is None:
            from safeops.environment.workload import WorkloadGenerator

            self._workload = WorkloadGenerator()
        return self._workload

    def _backend(self, spec: FaultSpec) -> SetContainerEnv:
        inj = spec.injection
        if (inj.backend, inj.action) != ("kubernetes-api", "set_container_env"):
            raise FaultSpecError(f"injection {inj.backend}/{inj.action} is not implemented")
        return SetContainerEnv(self._k(), self._ns())

    def load(self, fault_id: str) -> FaultSpec:
        return load_fault_spec(fault_id, self._faults_dir, self._topology)

    def inject(self, fault_id: str, fault_seed: int) -> InjectedFault:
        """Inject the fault deterministically from (fault_id, fault_seed); return InjectedFault."""
        spec = self.load(fault_id)
        return self.inject_spec(spec, fault_seed)

    def detection_attempts(self, spec: FaultSpec) -> int:
        """Attempts per detection window: ``window_s x rate_rps``, a whole number."""
        expr = spec.symptoms[0].parsed
        rate = self._load_workload().settings.rate_rps
        attempts = expr.window_s * rate
        if attempts != int(attempts) or attempts <= 0:
            raise FaultSpecError(
                f"symptom window {expr.window_s}s at {rate} req/s is not a whole number of attempts"
            )
        return int(attempts)

    def inject_spec(self, spec: FaultSpec, fault_seed: int) -> InjectedFault:
        index, variant = spec.variant_for(fault_seed)
        # Everything that can be refused is refused before the cluster changes (finding 4).
        self.detection_attempts(spec)
        previous = self._backend(spec).apply(spec.injection, variant)
        return InjectedFault(
            spec=spec,
            fault_seed=fault_seed,
            variant_index=index,
            variant=variant,
            previous_value=previous,
            injected_at=self._wall_clock(),
        )

    def verify_fault_present(
        self, fault: InjectedFault, timeout_s: float | None = None
    ) -> FaultVerification:
        """Confirm the symptom is observable before the agent starts (§11.2).

        Measures fixed-denominator windows of ``window_s x rate_rps`` scheduled attempts
        on the running workload, until one shows the declared expression holding, or until
        another window would overrun ``timeout_s``. Never judged from Prometheus.
        """
        timeout_s = self.verify_timeout_s if timeout_s is None else timeout_s
        symptom = fault.spec.symptoms[0]
        expr = symptom.parsed
        workload = self._load_workload()
        attempts = self.detection_attempts(fault.spec)
        started = self._clock()
        observations: list[SymptomObservation] = []
        while True:
            m = workload.measure_window(attempts)
            if not m.measured:
                raise FaultVerificationFailed(
                    f"detection window {m.window_id} not measured ({m.state}): {m.failure}"
                )
            errors = m.summary["server_error_responses"]
            value = errors / attempts
            obs = SymptomObservation(
                window_id=m.window_id,
                attempts=attempts,
                server_error_responses=errors,
                value=value,
                held=value > expr.threshold,
                observed_at=self._wall_clock(),
            )
            observations.append(obs)
            if obs.held:
                return FaultVerification(True, symptom, tuple(observations))
            if self._clock() - started + expr.window_s > timeout_s:
                worst = max(o.value for o in observations)
                return FaultVerification(
                    False,
                    symptom,
                    tuple(observations),
                    f"{symptom.expression!r} never held in {len(observations)} window(s) of"
                    f" {attempts} attempts within {timeout_s:g}s (max {worst:.3f})",
                )

    def clear(self, fault: InjectedFault) -> None:
        """Remove the injected fault by restoring the replaced value (a new revision).

        The run sequence never relies on this: the post-run reset recreates the app tier
        from desired state (decision 0013). It exists for repeated inject/clear checks.
        """
        self._backend(fault.spec).remove(fault.spec.injection, fault.variant, fault.previous_value)
