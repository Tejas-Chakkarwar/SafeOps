"""Action executor: the hard envelope, the six frozen operations, and what each write's
outcome means (design §8, §9.2, §11.6; decision 0025 D§10-D§13, M8B-1 amendment).

Enforces the hard envelope only. Never add an invariant check here (Rule 14): no safe range,
no request <= limit, no probe, health, selector-sanity or scope rule. Within the envelope it
executes what the gate approved, including actions the harm checker will score harmful.

`apply(action)`, in order:

1. **Structure.** The neutral RecommendedAction is re-validated with safeops.actions, the
   parser's and extractor's one structural contract. A malformed action is a harness bug
   (ValueError), raised before any envelope check, read or write: it never becomes a
   refusal.
2. **Common envelope, E1 -> E2 -> E3.** The first failure is REFUSED_BY_ENVELOPE, no steps.
3. **Per-action envelope** (D§11.2). Read-free checks first; the two pre-reads (rollback
   history, the resources container check) only after them.
4. **Plan.** Every request the action will send is built (the composite's restart value
   included) and preflighted together, before the first write. A preflight refusal raises.
5. **Write and classify.** One write at a time; each semantic reply is classified, and an
   ambiguous one is reconciled with a bounded read. Once any write may have been sent,
   nothing but an interrupt escapes: every outcome, a harness bug included, becomes a
   truthful ExecutionResult that keeps the execution evidence.
6. **Finish.** Every ExecutionResult apply() returns passes through one completion seam
   (`_finish`), which attempts exactly one `final` audit event. A failed final append
   can't change or suppress the result. A raising apply() produces no result and no final.

**Audit (M8B-2, D§11.8).** One sink, shared with the writer and bound to the current run
before the action starts (`bind_run`). apply() checks that binding first, before any envelope
check, read or write (HarnessMisuse). Every executor read attempt is audited: a failed read
entry never changes the read's result. The writer audits its own writes.

Every object a transport returns (a write's response, a read-back, a pre-write read, each
listed ReplicaSet) must be the object asked about: its kind, metadata.name and
metadata.namespace equal the request's. Anything else is not that object's evidence.

Its dependencies are injected (M8B-2 supplies the concrete transports without changing
`apply`): the loaded action semantics and bound topology build_runner shares with the M8A
read path, the configured namespace, a reader, a writer, a clock and a sleep.
"""

from __future__ import annotations

import copy
import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import Enum

from safeops.actions import exact_int, is_dns_label, params_problem, target_problem
from safeops.cluster import GuardError
from safeops.domain import RecommendedAction
from safeops.enums import ActionType, ExecutionStatus
from safeops.executor import quantity
from safeops.executor.audit import (
    AuditPersistenceError,
    ExecutorAudit,
    HarnessMisuse,
    keep_evidence,
)
from safeops.executor.result import (
    APPLIED,
    FAILED,
    SKIPPED,
    ExecutionResult,
    ExecutionStepResult,
    bounded,
)
from safeops.executor.transport import (
    Check,
    PatchType,
    PreparedWrite,
    ReaderTransport,
    ReadOutcome,
    ReadReply,
    WriteClass,
    WriteReply,
    WriterTransport,
    canonical_json,
    prepared,
)
from safeops.policy import ActionSemantics
from safeops.stubs import NotImplementedStage
from safeops.topology import ResourceSpec, ServiceTopology

# Every read, pre-write or reconciliation (D§11.6): at most 3 attempts, a 10 s timeout each,
# 2 s apart. The first successful read decides. No other retry exists, and no write retries.
READ_ATTEMPTS = 3
READ_TIMEOUT_S = 10
READ_RETRY_SLEEP_S = 2

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
POD_TEMPLATE_HASH = "pod-template-hash"
# The executor's units and Kubernetes' quantity suffixes (decision 0011).
RESOURCE_UNITS = {"cpu_millis": ("cpu", "m"), "memory_mib": ("memory", "Mi")}

# Harness bugs a transport may raise (D§11.3, D§11.6): a stage not implemented yet, a
# context-guard refusal, and use of the executor outside its run lifecycle.
#
# PREMISE: every member is guaranteed pre-send. NotImplementedStage is raised by a stub
# before it does anything; GuardError by the context guard before any command runs. So a
# write that raised one was never sent.
#
# HarnessMisuse currently has exactly one semantic raise source: the audit sink's
# unbound-run append guard (safeops/executor/audit.py, JsonlExecutorAudit._bound_run_id).
# Normal action lifecycle binds before the first executor-owned audited operation. The sink
# has no unbind operation. bind_run changes binding only between runs. Therefore an unbound
# HarnessMisuse is normally reachable only before a correct action's first possible send:
# apply() checks the binding first, and the writer's before_write append runs before its
# patch. Once a send is possible, the writer and the final seam consume any late audit
# exception themselves (audit.keep_evidence), so none reaches _attempt. Any future
# HarnessMisuse raise site must independently re-prove this pre-send property before being
# added.
#
# - Before any write may have been sent, a member stays loud: it propagates out of apply()
#   (preflight, a pre-write read, or the plan's first write).
# - After a write may have been sent, execution evidence must survive:
#   - at a later write, a member means that write was not attempted. The composite's step 2
#     is then `skipped` (PARTIALLY_APPLIED), and that mapping is truthful ONLY because of
#     the premise;
#   - in a reconciliation read, a member is one unavailable attempt inside the bounded
#     retry loop, like any other reader error.
#
# Adding a member requires re-evaluating the `skipped` mapping. An exception that can be
# raised after a request was sent MUST NOT be added here without changing those semantics.
HARNESS_BUGS = (NotImplementedStage, GuardError, HarnessMisuse)


class ExecutorConfigurationError(ValueError):
    """The loaded configuration can't say what an action means (a ConfigMap without exactly
    one owning Deployment): a harness bug, raised before any write, never a classification."""


class _Outcome(Enum):
    CONFIRMED = "confirmed"  # the requested postcondition was shown, by response or read
    REJECTED = "rejected"  # Kubernetes refused the content (422 Invalid)
    FAILED = "failed"  # attempted; success not confirmed
    NOT_ATTEMPTED = "not_attempted"  # never sent


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refused(reason: str) -> ExecutionResult:
    return ExecutionResult(ExecutionStatus.REFUSED_BY_ENVELOPE, bounded(reason), ())


def _unavailable(reason: str) -> ExecutionResult:
    """No write was attempted: a pre-write read couldn't establish what the envelope needs."""
    return ExecutionResult(ExecutionStatus.INFRASTRUCTURE_FAILURE, bounded(reason), ())


def _at(obj: object, *path: str) -> object:
    """obj[path[0]][path[1]]..., or None if any level is missing or not a mapping."""
    for key in path:
        if type(obj) is not dict:
            return None
        obj = obj.get(key)
    return obj


def _show(value: object) -> str:
    """repr(value), total: an integer too long for Python's str conversion (over 4300
    digits) is described by its size instead of raising."""
    try:
        return repr(value)
    except ValueError:
        return f"<an integer of {value.bit_length()} bits>"


def _identity(obj: object) -> tuple[object, object, object]:
    return (_at(obj, "kind"), _at(obj, "metadata", "name"), _at(obj, "metadata", "namespace"))


def _identity_problem(obj: object, kind: str, name: str, namespace: str) -> str | None:
    """None if `obj` is kind/name in namespace; otherwise what it is instead."""
    got = _identity(obj)
    if got == (kind, name, namespace):
        return None
    shown = ", ".join(_show(v) for v in got)
    return f"expected {kind}/{name} in {namespace!r}, got (kind, name, namespace) = ({shown})"


def render_configmap_value(value: object) -> str:
    """The ConfigMap data string for a params value (D§11.2): a string as is, an integer as
    str(i), a finite float as repr(f)."""
    if type(value) is str:
        return value
    if exact_int(value):
        return str(value)
    return repr(value)


def _named(containers: list, name: str) -> list[dict]:
    return [c for c in containers if type(c) is dict and c.get("name") == name]


def postcondition_holds(request: PreparedWrite, obj: object) -> bool:
    """Does `obj` (a returned or read-back object) show what `request` asked for (D§11.6)?
    Total: a missing, malformed or unrecognized value is simply "not shown", and so is any
    object other than the requested one (its kind, name and namespace must match)."""
    if _identity_problem(obj, request.kind, request.name, request.namespace) is not None:
        return False
    expected = request.postcondition.expected()
    check = request.postcondition.check
    if check is Check.REPLICAS:
        replicas = _at(obj, "spec", "replicas")
        return exact_int(replicas) and replicas == expected
    if check is Check.RESTART_ANNOTATION:
        value = _at(obj, "spec", "template", "metadata", "annotations", RESTART_ANNOTATION)
        return type(value) is str and value == expected
    if check is Check.TEMPLATE:
        # PROVISIONAL (M8B-1): the frozen D§11.6 comparison. M8B-2 verifies it against real
        # server defaulting and amends decision 0025 before narrowing it.
        template = _at(obj, "spec", "template")
        return type(template) is dict and canonical_json(template) == canonical_json(expected)
    if check is Check.CONFIGMAP_DATA:
        data = _at(obj, "data")
        return type(data) is dict and all(data.get(k) == v for k, v in expected.items())
    if check is Check.SELECTOR:
        selector = _at(obj, "spec", "selector")
        return type(selector) is dict and selector == expected
    if check is Check.RESOURCES:
        containers = _at(obj, "spec", "template", "spec", "containers")
        if type(containers) is not list:
            return False
        named = _named(containers, expected["container"])
        if len(named) != 1:
            return False
        for section in ("requests", "limits"):
            for field, value in expected.get(section, {}).items():
                quantity_key = RESOURCE_UNITS[field][0]
                got = _at(named[0], "resources", section, quantity_key)
                if field == "cpu_millis":
                    if not quantity.cpu_is(got, value):
                        return False
                elif not quantity.memory_is(got, value):
                    return False
        return True
    raise AssertionError(f"no postcondition for {check!r}")


class ActionExecutor:
    def __init__(
        self,
        *,
        semantics: ActionSemantics,
        topology: ServiceTopology,
        namespace: str,
        reader: ReaderTransport,
        writer: WriterTransport,
        audit: ExecutorAudit,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(semantics, ActionSemantics):
            raise TypeError(f"the executor needs the loaded ActionSemantics, not {semantics!r}")
        if not isinstance(topology, ServiceTopology):
            raise TypeError(f"the executor needs the bound ServiceTopology, not {topology!r}")
        if not is_dns_label(namespace):
            raise ValueError(f"the configured namespace must be a DNS-1123 label: {namespace!r}")
        if not isinstance(reader, ReaderTransport):
            raise TypeError(f"{type(reader).__name__} is not a ReaderTransport")
        if not isinstance(writer, WriterTransport):
            raise TypeError(f"{type(writer).__name__} is not a WriterTransport")
        if not isinstance(audit, ExecutorAudit):
            raise TypeError(f"{type(audit).__name__} is not an ExecutorAudit")
        # One sink: the writer audits its writes into the same object the executor binds.
        if getattr(writer, "audit", audit) is not audit:
            raise ValueError("the writer and the executor must share one audit sink")
        self.semantics = semantics
        self.topology = topology
        self.namespace = namespace
        self.reader = reader
        self.writer = writer
        self.audit = audit
        self._clock = clock
        self._sleep = sleep
        self._last_restart: datetime | None = None

    def bind_run(self, run_id: str) -> None:
        """Bind the shared audit sink to the run about to apply an action (D§11.8). Between
        runs only; apply(action) is unchanged."""
        self.audit.bind_run(run_id)

    def apply(self, action) -> ExecutionResult:
        """Apply a gate-approved action within the hard envelope; return an ExecutionResult.

        Raises only for harness bugs, and only before any write may have been sent: a
        structurally invalid action (ValueError), an unbound audit sink (HarnessMisuse), a
        configuration defect (ExecutorConfigurationError), a preflight refusal from the
        writer, a pre-write reader error, and a HARNESS_BUGS exception from the first write.
        After a possible send nothing but an interrupt escapes; every outcome is classified.

        Every result returned passes through `_finish` (one final audit attempt); a raise
        never does.
        """
        return self._finish(self._apply(action))

    def _finish(self, result: ExecutionResult) -> ExecutionResult:
        """The one completion seam: exactly one final audit append for a produced result. The
        result is already authoritative; a failed append can't change or suppress it, and is
        not retried."""
        keep_evidence(lambda: self.audit.final(result))
        return result

    def _apply(self, action) -> ExecutionResult:
        if not isinstance(action, RecommendedAction):
            raise TypeError(f"apply takes a RecommendedAction, not {type(action)}")
        problem = target_problem(action.target.model_dump()) or params_problem(
            action.type, action.params
        )
        if problem is not None:
            raise ValueError(f"apply takes a structurally valid action: {problem}")
        self.audit.require_bound()  # before any envelope check, read or write

        refusal = self._common_envelope(action)
        if refusal is not None:
            return _refused(refusal)
        spec = self.topology.resource(action.target.kind, action.target.name)
        plan = _PLANNERS[action.type](self, action, spec)
        if isinstance(plan, ExecutionResult):  # refused, or a pre-write read failed
            return plan
        if tuple(w.operation for w in plan) != self.semantics[action.type].steps:
            raise ExecutorConfigurationError(f"{action.type} planned {plan}, not its steps")
        self.writer.preflight(plan)
        return self._execute(plan)

    # --- the hard envelope ---------------------------------------------------------------------

    def _common_envelope(self, action: RecommendedAction) -> str | None:
        """E1 -> E2 -> E3 (D§11.1). The first failing check's reason, or None."""
        t = action.target
        kind = self.semantics[action.type].target_kind
        if t.kind != kind:
            return f"E1: {action.type} acts on a {kind}, not {t.kind}/{t.name}"
        if t.ns != self.namespace:
            return f"E2: {t.kind}/{t.name} is in namespace {t.ns!r}, not {self.namespace!r}"
        spec = self.topology.resource(t.kind, t.name)
        if spec is None:
            return f"E3: {t.kind}/{t.name} is not in the ownership map"
        if t.kind == "ConfigMap" and not spec.approved_keys:
            return f"E3: ConfigMap/{t.name} has no approved keys"
        return None

    # --- planning: every request, before any write -------------------------------------------

    def _restart_value(self) -> str:
        """A unique UTC RFC 3339 timestamp with microseconds. A repeated or earlier clock
        reading advances the last value by one microsecond: two restarts never share one."""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("the executor's clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        if self._last_restart is not None and now <= self._last_restart:
            now = self._last_restart + timedelta(microseconds=1)
        self._last_restart = now
        return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _restart(self, name: str) -> PreparedWrite:
        value = self._restart_value()
        body = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: value}}}}}
        return prepared(
            ActionType.RESTART_DEPLOYMENT,
            "Deployment",
            self.namespace,
            name,
            PatchType.MERGE,
            body,
            Check.RESTART_ANNOTATION,
            value,
        )

    def _plan_restart(self, action, spec):
        return (self._restart(action.target.name),)

    def _plan_scale(self, action, spec):
        replicas = action.params["replicas"]
        hard = spec.bounds["replicas"].hard
        if not hard.contains(replicas):
            return _refused(
                f"replicas {_show(replicas)} is outside Deployment/{spec.name}'s hard range"
                f" [{hard.lo}, {hard.hi}]"
            )
        body = {"spec": {"replicas": replicas}}
        return (
            prepared(
                ActionType.SCALE_DEPLOYMENT,
                "Deployment",
                self.namespace,
                spec.name,
                PatchType.MERGE,
                body,
                Check.REPLICAS,
                replicas,
            ),
        )

    def _plan_configmap(self, action, spec):
        for key, value in action.params.items():
            refusal = _configmap_key_refusal(spec, key, value)
            if refusal is not None:
                return refusal
        owners = [
            r
            for r in self.topology.resources.values()
            if r.kind == "Deployment" and r.owner == spec.owner
        ]
        if len(owners) != 1:
            raise ExecutorConfigurationError(
                f"ConfigMap/{spec.name}'s owner {spec.owner!r} has {len(owners)} Deployments;"
                " patch_configmap needs exactly one to restart"
            )
        data = {k: render_configmap_value(v) for k, v in action.params.items()}
        patch = prepared(
            ActionType.PATCH_CONFIGMAP,
            "ConfigMap",
            self.namespace,
            spec.name,
            PatchType.MERGE,
            {"data": data},
            Check.CONFIGMAP_DATA,
            data,
        )
        return (patch, self._restart(owners[0].name))  # step 2's value, before step 1 is sent

    def _plan_service(self, action, spec):
        selector = action.params["selector"]
        body = [{"op": "replace", "path": "/spec/selector", "value": selector}]
        return (
            prepared(
                ActionType.PATCH_SERVICE,
                "Service",
                self.namespace,
                spec.name,
                PatchType.JSON,
                body,
                Check.SELECTOR,
                selector,
            ),
        )

    def _plan_resources(self, action, spec):
        params = action.params
        ranges = _resource_range_refusal(params, spec)
        if ranges is not None:
            return ranges
        checked = self._container_check(spec.name, params["container"])
        if checked is not None:
            return checked
        resources = {}
        expected: dict = {"container": params["container"]}
        for section in ("requests", "limits"):
            if section in params:
                resources[section] = {
                    RESOURCE_UNITS[f][0]: f"{v}{RESOURCE_UNITS[f][1]}"
                    for f, v in sorted(params[section].items())
                }
                expected[section] = dict(params[section])
        container = {"name": params["container"], "resources": resources}
        body = {"spec": {"template": {"spec": {"containers": [container]}}}}
        return (
            prepared(
                ActionType.PATCH_DEPLOYMENT_RESOURCES,
                "Deployment",
                self.namespace,
                spec.name,
                PatchType.STRATEGIC,
                body,
                Check.RESOURCES,
                expected,
            ),
        )

    def _container_check(self, deployment: str, container: str) -> ExecutionResult | None:
        """The named container must already exist (D§11.2): a strategic merge by name could
        otherwise add one. None to continue; else the refusal or the read failure."""
        reply = self._read(
            lambda: self.reader.get("Deployment", deployment, self.namespace, READ_TIMEOUT_S),
            purpose="container_check",
            kind="Deployment",
            name=deployment,
        )
        if reply.outcome is ReadOutcome.UNAVAILABLE:
            return _unavailable(
                f"the container check could not read Deployment/{deployment}"
                f" after {READ_ATTEMPTS} attempts: {reply.diagnostic}"
            )
        if reply.outcome is ReadOutcome.ABSENT:
            return _refused(f"Deployment/{deployment} does not exist, so neither does {container}")
        wrong = _identity_problem(reply.obj, "Deployment", deployment, self.namespace)
        if wrong is not None:
            return _unavailable(f"the container check read the wrong object: {wrong}")
        containers = _at(reply.obj, "spec", "template", "spec", "containers")
        if type(containers) is not list:
            return _unavailable(f"Deployment/{deployment} as read has no container list")
        if not _named(containers, container):
            return _refused(f"container {container} does not exist in Deployment/{deployment}")
        return None

    def _plan_rollback(self, action, spec):
        name, revision = spec.name, action.params["to_revision"]
        reply = self._read(
            lambda: self.reader.get("Deployment", name, self.namespace, READ_TIMEOUT_S),
            purpose="rollback_history",
            kind="Deployment",
            name=name,
        )
        if reply.outcome is ReadOutcome.UNAVAILABLE:
            return _unavailable(
                f"the rollback history read could not read Deployment/{name}"
                f" after {READ_ATTEMPTS} attempts: {reply.diagnostic}"
            )
        shown = _show(revision)
        if reply.outcome is ReadOutcome.ABSENT:
            return _refused(f"Deployment/{name} does not exist: revision {shown} has no history")
        wrong = _identity_problem(reply.obj, "Deployment", name, self.namespace)
        if wrong is not None:
            return _unavailable(f"the rollback history read the wrong object: {wrong}")
        uid = _at(reply.obj, "metadata", "uid")
        if type(uid) is not str or not uid:
            return _unavailable(f"Deployment/{name} as read has no uid")
        listed = self._read(
            lambda: self.reader.list("ReplicaSet", self.namespace, READ_TIMEOUT_S),
            purpose="rollback_history",
            kind="ReplicaSet",
            name=None,
        )
        if listed.outcome is ReadOutcome.UNAVAILABLE:
            return _unavailable(
                f"the rollback history read could not list ReplicaSets"
                f" after {READ_ATTEMPTS} attempts: {listed.diagnostic}"
            )
        items = _at(listed.obj, "items")
        if type(items) is not list:
            return _unavailable("the ReplicaSet list as read has no items")
        # Absence from the list proves a revision absent only if the list is complete.
        if _at(listed.obj, "metadata", "continue") or _at(
            listed.obj, "metadata", "remainingItemCount"
        ):
            return _unavailable(
                "the ReplicaSet list as read is incomplete (a continue token or remaining items)"
            )
        for rs in items:
            if _at(rs, "metadata", "name") is None or (
                _at(rs, "kind"),
                _at(rs, "metadata", "namespace"),
            ) != ("ReplicaSet", self.namespace):
                return _unavailable(
                    f"the ReplicaSet list holds an object that is not a ReplicaSet in"
                    f" {self.namespace!r}: (kind, name, namespace) = {_identity(rs)!r}"
                )
        wanted = _decimal(revision)  # None: no annotation can hold it, so nothing matches
        matches = [
            rs
            for rs in items
            if _controlled_by(rs, name, uid)
            and _at(rs, "metadata", "annotations", REVISION_ANNOTATION) == wanted
            and wanted is not None
        ]
        if not matches:
            return _refused(f"revision {shown} is not in Deployment/{name}'s history")
        if len(matches) > 1:
            return _unavailable(f"{len(matches)} ReplicaSets claim revision {shown} of {name}")
        template = _at(matches[0], "spec", "template")
        if type(template) is not dict:
            return _unavailable(f"revision {shown}'s ReplicaSet has no pod template")
        cleaned = copy.deepcopy(template)  # never change the object as it was read
        labels = _at(cleaned, "metadata", "labels")
        if type(labels) is dict:
            labels.pop(POD_TEMPLATE_HASH, None)
        body = [{"op": "replace", "path": "/spec/template", "value": cleaned}]
        return (
            prepared(
                ActionType.ROLLBACK_DEPLOYMENT,
                "Deployment",
                self.namespace,
                name,
                PatchType.JSON,
                body,
                Check.TEMPLATE,
                cleaned,
            ),
        )

    # --- reads -------------------------------------------------------------------------------

    def _read(
        self,
        call: Callable[[], ReadReply],
        *,
        purpose: str,
        kind: str,
        name: str | None,
        request: PreparedWrite | None = None,
        after_write: bool = False,
    ) -> ReadReply:
        """At most READ_ATTEMPTS attempts, READ_RETRY_SLEEP_S apart; the first FOUND or
        ABSENT decides. Before any write a reader error propagates (a harness bug, nothing
        sent). After a possible write any non-interrupt reader error, a HARNESS_BUGS one
        included, makes THAT attempt unavailable and the loop goes on: it never ends
        reconciliation early and never escapes. Each attempt is audited (_audit_read)."""
        reply = ReadReply(ReadOutcome.UNAVAILABLE, diagnostic="no read was attempted")
        for attempt in range(READ_ATTEMPTS):
            if attempt:
                self._sleep(READ_RETRY_SLEEP_S)
            reply = self._one_read(call, after_write)
            self._audit_read(purpose, attempt + 1, kind, name, reply, request, after_write)
            if reply.outcome is not ReadOutcome.UNAVAILABLE:
                return reply
        return reply

    @staticmethod
    def _one_read(call, after_write: bool) -> ReadReply:
        if not after_write:
            reply = call()
            if not isinstance(reply, ReadReply):
                raise TypeError(f"the reader returned {type(reply).__name__}, not a ReadReply")
            return reply
        try:
            reply = call()
        except Exception as exc:
            return ReadReply(ReadOutcome.UNAVAILABLE, diagnostic=f"{type(exc).__name__}: {exc}")
        if not isinstance(reply, ReadReply):
            return ReadReply(ReadOutcome.UNAVAILABLE, diagnostic="the reader returned no reply")
        return reply

    def _audit_read(self, purpose, attempt, kind, name, reply, request, after_write) -> None:
        """One read event (D§11.7, D§11.8). The read already happened: a failed entry never
        changes its result. Before any write an unbound sink stays loud (apply() checked the
        binding first, so this is defensive); after one, nothing the entry does can escape.
        Reconciliation entries carry the compared fields only, never the object."""

        def append() -> None:
            fields = {
                "purpose": purpose,
                "attempt": attempt,
                "kind": kind,
                "namespace": self.namespace,
                "name": name,
                "outcome": reply.outcome.value,
                "diagnostic": reply.diagnostic,
            }
            if request is not None:
                fields["reconciliation"] = _reconciliation_view(request, reply)
            self.audit.read(**fields)

        if after_write:
            keep_evidence(append)
            return
        try:
            append()
        except AuditPersistenceError:
            return

    # --- writes ------------------------------------------------------------------------------

    def _execute(self, plan: tuple[PreparedWrite, ...]) -> ExecutionResult:
        """Send the planned writes in order and classify (D§11.5, D§11.6). An earlier step
        known applied dominates: whatever happens next, the result is partially_applied."""
        done: list[ExecutionStepResult] = []
        for index, request in enumerate(plan):
            # Every earlier request was sent or may have been: past the first, a write may
            # already have reached the cluster.
            outcome, error = self._attempt(request, after_possible_write=index > 0)
            if outcome is _Outcome.CONFIRMED:
                done.append(ExecutionStepResult(request.operation, APPLIED))
                continue
            if done:
                status = SKIPPED if outcome is _Outcome.NOT_ATTEMPTED else FAILED
                step_error = None if status == SKIPPED else error
                done.append(ExecutionStepResult(request.operation, status, step_error))
                return ExecutionResult(ExecutionStatus.PARTIALLY_APPLIED, error, tuple(done))
            if outcome is _Outcome.NOT_ATTEMPTED:
                return ExecutionResult(ExecutionStatus.INFRASTRUCTURE_FAILURE, error, ())
            failed = (ExecutionStepResult(request.operation, FAILED, error),)
            if outcome is _Outcome.REJECTED:
                return ExecutionResult(ExecutionStatus.REJECTED_INVALID_ACTION, error, failed)
            return ExecutionResult(ExecutionStatus.INFRASTRUCTURE_FAILURE, error, failed)
        return ExecutionResult(ExecutionStatus.APPLIED, None, tuple(done))

    def _attempt(
        self, request: PreparedWrite, *, after_possible_write: bool
    ) -> tuple[_Outcome, str | None]:
        """One write, classified from its semantic reply. Never raises for what happened
        after the request may have been sent: a writer error or a reply of the wrong type is
        ambiguous, and reconciled like any other.

        A HARNESS_BUGS exception means, by the HARNESS_BUGS premise, that this request was
        never sent. Before any possible write it propagates. After one it must not erase the
        evidence, so it is NOT_ATTEMPTED: the composite's step 2 becomes `skipped`. The
        diagnostic names the real cause: a HarnessMisuse is a lifecycle bug (an unbound
        audit sink), not an adapter contract violation."""
        what = f"{request.operation.value} {request.kind}/{request.name}"
        try:
            reply = self.writer.write(request)
        except HARNESS_BUGS as exc:
            if not after_possible_write:
                raise
            return _Outcome.NOT_ATTEMPTED, bounded(_not_sent(what, exc))
        except Exception as exc:
            reply = WriteReply(WriteClass.AMBIGUOUS, diagnostic=f"{type(exc).__name__}: {exc}")
        if not isinstance(reply, WriteReply):
            reply = WriteReply(WriteClass.AMBIGUOUS, diagnostic="the writer returned no reply")
        detail = reply.diagnostic or "no diagnostic"
        cls = reply.semantic_class
        if cls is WriteClass.SUCCESS:
            if postcondition_holds(request, reply.returned_object):
                return _Outcome.CONFIRMED, None
            return self._reconcile(request, f"{what}: the response did not show the change")
        if cls is WriteClass.INVALID_ACTION:
            return _Outcome.REJECTED, bounded(f"{what}: Kubernetes rejected it: {detail}")
        if cls is WriteClass.DEFINITE_FAILURE:
            return _Outcome.FAILED, bounded(f"{what}: the request failed: {detail}")
        if cls is WriteClass.NOT_ATTEMPTED:
            return _Outcome.NOT_ATTEMPTED, bounded(f"{what}: not attempted: {detail}")
        return self._reconcile(request, f"{what}: ambiguous response: {detail}")

    def _reconcile(self, request: PreparedWrite, why: str) -> tuple[_Outcome, str | None]:
        """Read the object back (bounded): confirmed, absent or unavailable. Never a write."""
        reply = self._read(
            lambda: self.reader.get(request.kind, request.name, request.namespace, READ_TIMEOUT_S),
            purpose="reconciliation",
            kind=request.kind,
            name=request.name,
            request=request,
            after_write=True,
        )
        if reply.outcome is ReadOutcome.UNAVAILABLE:
            return _Outcome.FAILED, bounded(
                f"{why}; reconciliation could not read it after {READ_ATTEMPTS} attempts"
                " (unresolved)"
            )
        if reply.outcome is ReadOutcome.FOUND and postcondition_holds(request, reply.obj):
            return _Outcome.CONFIRMED, None
        return _Outcome.FAILED, bounded(f"{why}; reconciliation found the change absent")


def _not_sent(what: str, exc: Exception) -> str:
    """The diagnostic for a HARNESS_BUGS refusal after a possible write: its real cause."""
    cause = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, HarnessMisuse):
        return f"{what}: not attempted before send: {cause}"
    return (
        f"{what}: not attempted before send: the writer refused, an adapter contract"
        f" violation (preflight owns it): {cause}"
    )


def _reconciliation_view(request: PreparedWrite, reply: ReadReply) -> dict:
    """What a reconciliation entry records (D§11.8): the outcome (confirmed, absent or
    unavailable) and the compared fields only. Total; never the whole object."""
    if reply.outcome is ReadOutcome.UNAVAILABLE:
        return {"outcome": "unavailable"}
    if reply.outcome is ReadOutcome.ABSENT:
        return {"outcome": "absent"}
    obj = reply.obj
    holds = postcondition_holds(request, obj)
    check = request.postcondition.check
    if check is Check.REPLICAS:
        observed = _at(obj, "spec", "replicas")
    elif check is Check.RESTART_ANNOTATION:
        observed = _at(obj, "spec", "template", "metadata", "annotations", RESTART_ANNOTATION)
    elif check is Check.CONFIGMAP_DATA:
        data = _at(obj, "data")
        keys = request.postcondition.expected()
        observed = {k: data.get(k) for k in keys} if type(data) is dict else None
    elif check is Check.SELECTOR:
        observed = _at(obj, "spec", "selector")
    elif check is Check.RESOURCES:
        containers = _at(obj, "spec", "template", "spec", "containers")
        name = request.postcondition.expected()["container"]
        named = _named(containers, name) if type(containers) is list else []
        observed = _at(named[0], "resources") if len(named) == 1 else None
    else:  # the template: its digest, never the template itself
        template = _at(obj, "spec", "template")
        observed = (
            "sha256:" + hashlib.sha256(canonical_json(template).encode()).hexdigest()
            if type(template) is dict
            else None
        )
    return {
        "outcome": "confirmed" if holds else "absent",
        "identity": list(_identity(obj)),
        "check": check.value,
        "observed": observed,
    }


def _configmap_key_refusal(spec: ResourceSpec, key: str, value: object) -> ExecutionResult | None:
    """One proposed key against the target's approved_keys (D§11.2): approved; an enum key a
    string in its enum; a numeric key a JSON number, never a string, within its hard_range."""
    rule = spec.approved_keys.get(key)
    if rule is None:
        return _refused(f"{key} is not an approved key of ConfigMap/{spec.name}")
    if "enum" in rule:
        if type(value) is not str or value not in rule["enum"]:
            return _refused(f"{key}={_show(value)} is not one of {list(rule['enum'])}")
        return None
    if type(value) not in (int, float):
        return _refused(f"{key} is numeric: {_show(value)} is not a JSON number")
    lo, hi = rule["hard_range"]
    if not lo <= value <= hi:
        return _refused(f"{key}={_show(value)} is outside its hard range [{lo}, {hi}]")
    return None


def _resource_range_refusal(params: dict, spec: ResourceSpec) -> ExecutionResult | None:
    """Each given request and limit against its dimension's hard range, independently
    (decision 0011, D§12). Never request <= limit, never a safe range."""
    for section in ("requests", "limits"):
        for field, value in sorted(params.get(section, {}).items()):
            hard = spec.bounds[field].hard
            if not hard.contains(value):
                return _refused(
                    f"{section}.{field} {_show(value)} is outside Deployment/{spec.name}'s"
                    " hard range"
                    f" [{hard.lo}, {hard.hi}]"
                )
    return None


def _decimal(revision: int) -> str | None:
    """The revision annotation's text for `revision`, or None if no annotation can hold it
    (beyond Python's integer-string digit limit)."""
    try:
        return str(revision)
    except ValueError:
        return None


def _controlled_by(rs: object, name: str, uid: str) -> bool:
    """Is this ReplicaSet controlled by Deployment `name` (that exact object, by uid)?"""
    refs = _at(rs, "metadata", "ownerReferences")
    if type(refs) is not list:
        return False
    return any(
        type(ref) is dict
        and ref.get("controller") is True
        and ref.get("kind") == "Deployment"
        and ref.get("name") == name
        and ref.get("uid") == uid
        for ref in refs
    )


_PLANNERS = {
    ActionType.ROLLBACK_DEPLOYMENT: ActionExecutor._plan_rollback,
    ActionType.RESTART_DEPLOYMENT: ActionExecutor._plan_restart,
    ActionType.SCALE_DEPLOYMENT: ActionExecutor._plan_scale,
    ActionType.PATCH_CONFIGMAP: ActionExecutor._plan_configmap,
    ActionType.PATCH_SERVICE: ActionExecutor._plan_service,
    ActionType.PATCH_DEPLOYMENT_RESOURCES: ActionExecutor._plan_resources,
}
