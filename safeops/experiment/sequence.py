"""The §5 run sequence as data, for ``safeops run --dry-run``.

The dry-run is static: it inspects classes, never instances. It builds no Kubernetes
client, contacts no LLM provider, and has no side effects (decision 0004). Each row's
status comes from the real method, so the listing can't drift from what is implemented.
tests/test_run_sequence.py checks that this order matches ``ExperimentRunner.run``.
"""

from __future__ import annotations

from dataclasses import dataclass

from safeops.agent.loop import Agent
from safeops.agent.parser import ProposalParser
from safeops.environment.manager import EnvironmentManager
from safeops.environment.workload import WorkloadGenerator
from safeops.executor.executor import ActionExecutor
from safeops.experiment.records import RunRecordWriter
from safeops.experiment.runner import ExperimentRunner
from safeops.faults.alerts import AlertGenerator
from safeops.faults.injector import FaultInjector
from safeops.features.extractor import RiskFeatureExtractor
from safeops.gate.always_act import AlwaysActGate
from safeops.gate.shadow import ShadowGateScorer
from safeops.stubs import stub_milestone
from safeops.verify.effectiveness import EffectivenessVerifier
from safeops.verify.harm import HarmChecker


@dataclass(frozen=True)
class Stage:
    step: str
    owner: type
    method: str
    when: str = ""

    @property
    def qualname(self) -> str:
        return f"{self.owner.__name__}.{self.method}"

    @property
    def milestone(self) -> int | None:
        """Milestone the stage is waiting on, or None once implemented."""
        return stub_milestone(getattr(self.owner, self.method))


_VALID = "valid_action only"
_EXECUTE = "gate = execute only"

RUN_SEQUENCE: tuple[Stage, ...] = (
    Stage("reset environment", EnvironmentManager, "reset_environment"),
    Stage("verify clean state", EnvironmentManager, "verify_clean_state"),
    Stage("handle pre-run clean state", ExperimentRunner, "_require_pre_run_clean"),
    Stage("start workload", WorkloadGenerator, "start_workload"),
    Stage("verify steady state", WorkloadGenerator, "verify_steady_state"),
    Stage("handle steady state", ExperimentRunner, "_require_steady_state"),
    Stage("capture pre-fault snapshot", EnvironmentManager, "capture_snapshot"),
    Stage("inject fault", FaultInjector, "inject"),
    Stage("verify fault present", FaultInjector, "verify_fault_present"),
    Stage("handle fault verification", ExperimentRunner, "_require_fault_present"),
    Stage("capture post-fault snapshot", EnvironmentManager, "capture_snapshot"),
    Stage("build alert (symptoms only)", AlertGenerator, "build_alert"),
    Stage("build runtime context", ExperimentRunner, "_runtime_context"),
    Stage("agent investigates", Agent, "investigate"),
    Stage("parse proposal", ProposalParser, "parse"),
    Stage("route on proposal_status", ExperimentRunner, "_gate_eligible_action"),
    Stage("extract frozen feature vector", RiskFeatureExtractor, "extract", _VALID),
    Stage("score shadow gates", ShadowGateScorer, "score", _VALID),
    Stage("gate decides", AlwaysActGate, "decide", _VALID),
    Stage("route on gate decision", ExperimentRunner, "_should_execute", _VALID),
    Stage("execute action", ActionExecutor, "apply", _EXECUTE),
    Stage("determine executed action", ExperimentRunner, "_executed_action", _EXECUTE),
    Stage("wait stabilization window", EnvironmentManager, "wait_stabilization_window"),
    Stage("capture post-action snapshot", EnvironmentManager, "capture_snapshot"),
    Stage("verify effectiveness", EffectivenessVerifier, "verify"),
    Stage("check harm", HarmChecker, "check"),
    Stage("stop workload", WorkloadGenerator, "stop_workload"),
    Stage("reset environment", EnvironmentManager, "reset_environment"),
    Stage("verify clean state", EnvironmentManager, "verify_clean_state"),
    Stage("handle post-run clean state", ExperimentRunner, "_post_run_clean"),
    Stage("finalize run record", RunRecordWriter, "finalize"),
)


def first_stub(sequence: tuple[Stage, ...] = RUN_SEQUENCE) -> Stage | None:
    return next((s for s in sequence if s.milestone is not None), None)


def render_dry_run(sequence: tuple[Stage, ...] = RUN_SEQUENCE) -> str:
    header = ("#", "step", "implemented by", "status", "when")
    rows = [
        (
            str(i),
            s.step,
            s.qualname,
            "implemented" if s.milestone is None else f"stub, Milestone {s.milestone}",
            s.when,
        )
        for i, s in enumerate(sequence, start=1)
    ]
    widths = [max(len(r[c]) for r in [header, *rows]) for c in range(len(header))]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()

    stubs = sum(1 for s in sequence if s.milestone is not None)
    first = first_stub(sequence)
    lines = [
        "safeops run: pipeline inspection only (no experiment, cluster, or provider contact)",
        "",
        fmt(header),
        *(fmt(r) for r in rows),
        "",
        f"{len(sequence)} stages: {len(sequence) - stubs} implemented, {stubs} stubs.",
    ]
    if first is not None:
        lines.append(f"A real run stops first at {first.qualname} (Milestone {first.milestone}).")
    return "\n".join(lines)
