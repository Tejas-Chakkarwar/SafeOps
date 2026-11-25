"""Stored trajectory evidence: guarded reads, the latest observation, and the semantic support
predicates (decision 0025 D§5, D§7.3).

**The representation.** A trajectory is a list of `safeops.domain.TrajectoryStep`. Its
`args` and `result` are the JSON-safe values the run record stores: dicts, lists and
scalars, never live tool-contract models. The extractor reads every step through
`stored_form`, the record's own JSON round trip, so run time and write time see one form.
A result is the envelope `{"status": "ok", "data": {...}}` or `{"status": "error", ...}`.
So every predicate here can be recomputed later from a stored record, exactly as it ran.

**Guarded reads.** Nothing here imports safeops.tools: importing any of its modules would
initialize safeops.tools, whose package imports the kubectl-backed tools (and so
safeops.cluster). The few fields the predicates need are read with explicit type checks. A
missing key, a wrong type, a non-ok envelope or a malformed element establishes nothing; it
never raises. Bools are excluded from integers (`type(x) is int`).

**Corroboration, not coverage** (§11.5). A successful call earns nothing by itself: a
requirement holds only when the latest matching observation's structured content
establishes its proposition. Anything else is "not established", which raises risk in both
features that use it (D§7.2's truncation-direction invariant), so no tri-state is needed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType

from safeops.domain import RecommendedAction, TrajectoryStep
from safeops.enums import ActionType

_A = ActionType


def stored_form(trajectory: Sequence[TrajectoryStep]) -> list[TrajectoryStep]:
    """The trajectory exactly as the run record stores it: each step dumped in JSON mode
    (the writer's `_plain`), serialized with `allow_nan=False` and validated back, as the
    writer and the schema do. Extraction reads only this form, so a live value that isn't
    plain JSON (a tuple where the record will hold a list) can't be read one way at run time
    and another from the record at write time or later. A value with no stored form at all
    (an arbitrary object, a mapping proxy) is a harness bug: it raises, as the writer would."""
    if not isinstance(trajectory, list | tuple) or not all(
        isinstance(s, TrajectoryStep) for s in trajectory
    ):
        raise TypeError("extract takes the stored trajectory: a list of TrajectoryStep")
    try:
        return [
            TrajectoryStep.model_validate_json(
                json.dumps(s.model_dump(mode="json"), sort_keys=True, allow_nan=False)
            )
            for s in trajectory
        ]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"a trajectory step has no strict-JSON stored form: {exc}") from exc


def as_dict(value: object) -> dict | None:
    """`value` if it is a JSON object, else None."""
    return value if type(value) is dict else None


def nonneg_int(value: object) -> int | None:
    """`value` if it is a JSON integer (not a bool) >= 0, else None."""
    return value if type(value) is int and value >= 0 else None


def ok_data(step: TrajectoryStep) -> dict | None:
    """The `data` of a successful stored envelope, or None."""
    result = as_dict(step.result)
    if result is None or result.get("status") != "ok":
        return None
    return as_dict(result.get("data"))


def latest(
    trajectory: Sequence[TrajectoryStep], tool: str, namespace: str, name: str
) -> dict | None:
    """The `data` of the latest successful matching observation (D§5), or None.

    Matching: exactly this tool, exactly the arguments {namespace, name}, an ok envelope
    whose data is an object. Latest: the greatest `TrajectoryStep.step`, the validated
    ordering key; never list position or a timestamp."""
    best: TrajectoryStep | None = None
    best_data: dict | None = None
    for step in trajectory:
        if step.tool != tool or step.args != {"namespace": namespace, "name": name}:
            continue
        data = ok_data(step)
        if data is None:
            continue
        if best is None or step.step > best.step:
            best, best_data = step, data
    return best_data


# --- the observations the predicates and blast_radius read ---------------------------------------


def observed_replicas(data: dict | None) -> int | None:
    """A found Deployment's replica count, or None (D§5.3's replica evidence)."""
    if data is None or data.get("found") is not True:
        return None
    return nonneg_int(data.get("replicas"))


def revision_healthy(data: dict | None, revision: object) -> bool:
    """D§7.3: the stored history lists `revision` with replicas > 0 and every replica ready.

    A revision absent from the bounded list is not healthy here, truncated or not."""
    if data is None or type(revision) is not int:
        return False
    revisions = data.get("revisions")
    if type(revisions) is not list:
        return False
    for entry in revisions:
        entry = as_dict(entry)
        if entry is None or type(entry.get("revision")) is not int:
            continue
        if entry["revision"] != revision:
            continue
        replicas, ready = nonneg_int(entry.get("replicas")), nonneg_int(entry.get("ready_replicas"))
        if replicas is not None and ready is not None and replicas > 0 and ready == replicas:
            return True
    return False


# --- requirement predicates (config/evidence_requirements.yaml names them) ----------------------

Predicate = Callable[[RecommendedAction, Sequence[TrajectoryStep], str], bool]


def _observe(tool: str, action: RecommendedAction, trajectory, namespace: str) -> dict | None:
    return latest(trajectory, tool, namespace, action.target.name)


def rollout_history_retrieved(action, trajectory, namespace) -> bool:
    data = _observe("get_rollout_history", action, trajectory, namespace)
    count = None if data is None else nonneg_int(data.get("revision_count"))
    return count is not None and count >= 1


def prior_revision_health_observed(action, trajectory, namespace) -> bool:
    data = _observe("get_rollout_history", action, trajectory, namespace)
    return revision_healthy(data, action.params.get("to_revision"))


def failure_correlated_with_current_revision(action, trajectory, namespace) -> bool:
    """Not establishable over the current tool contract (D§7.3): never satisfied in pre-v1."""
    return False


def target_deployment_state_observed(action, trajectory, namespace) -> bool:
    data = _observe("get_deployment", action, trajectory, namespace)
    return data is not None and data.get("found") is True


def current_replicas_observed(action, trajectory, namespace) -> bool:
    return observed_replicas(_observe("get_deployment", action, trajectory, namespace)) is not None


def target_container_resources_observed(action, trajectory, namespace) -> bool:
    data = _observe("get_deployment", action, trajectory, namespace)
    if data is None or data.get("found") is not True:
        return False
    containers = data.get("containers")
    if type(containers) is not list:
        return False
    wanted = action.params.get("container")
    return any(as_dict(c) is not None and c.get("name") == wanted for c in containers)


def target_keys_observed(action, trajectory, namespace) -> bool:
    data = _observe("get_configmap", action, trajectory, namespace)
    if data is None or data.get("found") is not True:
        return False
    entries = data.get("entries")
    if type(entries) is not list or not action.params:
        return False
    shown = {}
    for entry in entries:
        entry = as_dict(entry)
        if entry is not None and type(entry.get("key")) is str:
            shown[entry["key"]] = entry
    for key in action.params:
        entry = shown.get(key)
        if (
            entry is None
            or entry.get("redacted") is not False
            or type(entry.get("value")) is not str
        ):
            return False
    return True


def target_service_state_observed(action, trajectory, namespace) -> bool:
    data = _observe("get_service", action, trajectory, namespace)
    if data is None or data.get("found") is not True:
        return False
    return as_dict(data.get("selector")) is not None and (
        nonneg_int(data.get("ready_endpoint_count")) is not None
    )


# The closed, code-owned registry: requirement id -> (the action type it belongs to, its
# predicate). The evidence-requirements loader accepts only these ids, each under its own type.
REQUIREMENTS: Mapping[str, tuple[ActionType, Predicate]] = MappingProxyType(
    {
        "rollout_history_retrieved": (_A.ROLLBACK_DEPLOYMENT, rollout_history_retrieved),
        "prior_revision_health_observed": (_A.ROLLBACK_DEPLOYMENT, prior_revision_health_observed),
        "failure_correlated_with_current_revision": (
            _A.ROLLBACK_DEPLOYMENT,
            failure_correlated_with_current_revision,
        ),
        "target_deployment_state_observed": (
            _A.RESTART_DEPLOYMENT,
            target_deployment_state_observed,
        ),
        "current_replicas_observed": (_A.SCALE_DEPLOYMENT, current_replicas_observed),
        "target_container_resources_observed": (
            _A.PATCH_DEPLOYMENT_RESOURCES,
            target_container_resources_observed,
        ),
        "target_keys_observed": (_A.PATCH_CONFIGMAP, target_keys_observed),
        "target_service_state_observed": (_A.PATCH_SERVICE, target_service_state_observed),
    }
)


# --- prerequisite vocabulary ---------------------------------------------------------------------


def prior_revision_healthy(action, trajectory, namespace) -> bool:
    """Supported only for a rollback whose target revision the history shows healthy."""
    if action.type is not _A.ROLLBACK_DEPLOYMENT:
        return False
    return prior_revision_health_observed(action, trajectory, namespace)


PREREQUISITES: Mapping[str, Predicate] = MappingProxyType(
    {"prior_revision_healthy": prior_revision_healthy}
)
