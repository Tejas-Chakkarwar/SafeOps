"""Offline transports for the M8B-1 executor (decision 0025, M8B-1 amendment).

A FakeCluster holds JSON objects and applies the three patch types the executor sends
(merge, JSON-patch replace, strategic merge of containers by name) the way the API server
would, so a test can see what a request really does: a stale selector key surviving, a
container added by name. FakeWriter answers each write from a script of semantic outcomes;
FakeReader reads the cluster, failing as scripted. RecordingAudit is an in-memory audit sink
with the production sink's binding rule and scripted failures (M8B-2). Nothing here touches
Kubernetes or the filesystem.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from safeops.cluster import GuardError
from safeops.domain import ActionTarget, RecommendedAction
from safeops.enums import ActionType
from safeops.executor.audit import HarnessMisuse, canonical_run_id
from safeops.executor.executor import ActionExecutor
from safeops.executor.transport import PatchType, ReadOutcome, ReadReply, WriteClass, WriteReply
from safeops.policy import load_action_semantics
from safeops.stubs import NotImplementedStage
from tests.m8_fixtures import TOPOLOGY

SEMANTICS = load_action_semantics()
NS = "testbed"
RUN_A = "00000000-0000-4000-8000-00000000000a"
RUN_B = "00000000-0000-4000-8000-00000000000b"
ORDERS_UID = "uid-orders-1"


def _template(image: str, hash_: str) -> dict:
    return {
        "metadata": {
            "labels": {"app": "orders", "pod-template-hash": hash_},
            "annotations": {"team": "orders"},
        },
        "spec": {
            "containers": [
                {
                    "name": "orders",
                    "image": image,
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "500m", "memory": "512Mi"},
                    },
                }
            ]
        },
    }


def _rs(name: str, revision: str, image: str, *, controller=True, uid=ORDERS_UID, ns=NS) -> dict:
    return {
        "kind": "ReplicaSet",
        "metadata": {
            "name": name,
            "namespace": ns,
            "annotations": {"deployment.kubernetes.io/revision": revision},
            "ownerReferences": [
                {"kind": "Deployment", "name": "orders", "uid": uid, "controller": controller}
            ],
        },
        "spec": {"template": _template(image, f"hash-{name}")},
    }


def seed_objects(ns: str = NS) -> dict:
    """The testbed objects, each carrying its kind, name and namespace as the API server
    returns them (the executor checks all three)."""
    current = _template("orders:2", "hash-orders-rs2")
    del current["metadata"]["labels"]["pod-template-hash"]
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "orders", "namespace": ns, "uid": ORDERS_UID},
        "spec": {"replicas": 2, "template": current},
    }
    gateway = copy.deepcopy(deployment)
    gateway["metadata"].update(name="gateway", uid="uid-gateway")
    return {
        ("Deployment", "orders"): deployment,
        ("Deployment", "gateway"): gateway,
        ("ConfigMap", "db-pool-config"): {
            "kind": "ConfigMap",
            "metadata": {"name": "db-pool-config", "namespace": ns},
            "data": {"max_pool_size": "10", "connection_timeout_ms": "1000", "other": "kept"},
        },
        ("ConfigMap", "orders-config"): {
            "kind": "ConfigMap",
            "metadata": {"name": "orders-config", "namespace": ns},
            "data": {"log_level": "info"},
        },
        ("Service", "orders-svc"): {
            "kind": "Service",
            "metadata": {"name": "orders-svc", "namespace": ns},
            "spec": {"selector": {"app": "orders", "tier": "backend"}, "ports": [{"port": 80}]},
        },
        ("ReplicaSet", "orders-rs1"): _rs("orders-rs1", "1", "orders:1", ns=ns),
        ("ReplicaSet", "orders-rs2"): _rs("orders-rs2", "2", "orders:2", ns=ns),
        # Revision 3 exists only on a ReplicaSet orders doesn't control; revision 4 only on
        # one controlled by an earlier Deployment of the same name (another uid).
        ("ReplicaSet", "orders-stray"): _rs(
            "orders-stray", "3", "orders:3", controller=False, ns=ns
        ),
        ("ReplicaSet", "orders-old"): _rs("orders-old", "4", "orders:4", uid="uid-orders-0", ns=ns),
    }


def _merge(target: dict, patch: dict) -> None:
    """RFC 7386."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif type(value) is dict and type(target.get(key)) is dict:
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _strategic(target: dict, patch: dict) -> None:
    """Strategic merge as the API server does it for a pod template: containers merge by
    their merge key `name`, so an unknown name ADDS a container."""
    for key, value in patch.items():
        if key == "containers" and type(value) is list:
            existing = target.setdefault(key, [])
            for container in value:
                match = next((c for c in existing if c.get("name") == container["name"]), None)
                if match is None:
                    existing.append(copy.deepcopy(container))
                else:
                    _strategic(match, container)
        elif type(value) is dict and type(target.get(key)) is dict:
            _strategic(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _json_patch(target: dict, ops: list) -> None:
    for op in ops:
        assert op["op"] == "replace"
        *parents, last = op["path"].strip("/").split("/")
        node = target
        for part in parents:
            node = node[part]
        assert last in node, "JSON patch replace needs an existing path"
        node[last] = copy.deepcopy(op["value"])


class FakeCluster:
    def __init__(self, objects: dict | None = None, *, canonicalize: bool = False) -> None:
        self.objects = seed_objects() if objects is None else objects
        self.canonicalize = canonicalize

    def get(self, kind: str, name: str):
        obj = self.objects.get((kind, name))
        return None if obj is None else copy.deepcopy(obj)

    def apply(self, request) -> None:
        obj = self.objects[(request.kind, request.name)]
        body = request.body()
        if request.patch_type is PatchType.MERGE:
            _merge(obj, body)
        elif request.patch_type is PatchType.STRATEGIC:
            _strategic(obj, body)
        else:
            _json_patch(obj, body)
        if self.canonicalize:  # the API server's quantity canonical form: 1024Mi -> 1Gi
            for c in obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
                for section in c.get("resources", {}).values():
                    memory = section.get("memory", "")
                    if memory.endswith("Mi") and int(memory[:-2]) % 1024 == 0:
                        section["memory"] = f"{int(memory[:-2]) // 1024}Gi"
                    cpu = section.get("cpu", "")
                    if cpu.endswith("m") and int(cpu[:-1]) % 1000 == 0:
                        section["cpu"] = str(int(cpu[:-1]) // 1000)


class FakeWriter:
    """Each write takes the next scripted outcome (default "success"):

    success          applied; returns the object as stored
    stale            applied; returns the object as it was before (no confirmation)
    lie              not applied; claims success with the pre-write object
    invalid          422 Invalid: nothing written
    definite         403 Forbidden: nothing written
    not_attempted    never sent
    ambiguous        applied, then the response was lost (timeout)
    lost             not applied, and the response was lost
    raise            applied, then the transport raised
    junk             applied; returns something that isn't a WriteReply
    guard            never sent; raises GuardError (an adapter contract violation)
    stub             never sent; raises NotImplementedStage (a half-wired adapter)
    """

    def __init__(self, cluster: FakeCluster, script=(), preflight_raises=None, clock=None):
        self.cluster, self.script = cluster, list(script)
        self.preflight_raises = preflight_raises
        self.preflights: list[tuple] = []
        self.writes: list = []
        self.clock_calls_at_write: list[int] = []
        self._clock = clock

    def preflight(self, requests):
        self.preflights.append((tuple(requests), len(self.writes)))
        if self.preflight_raises is not None:
            raise self.preflight_raises

    def write(self, request):
        self.writes.append(request)
        if self._clock is not None:
            self.clock_calls_at_write.append(len(self._clock.calls))
        how = self.script.pop(0) if self.script else "success"
        if how == "guard":
            raise GuardError("the context guard refused: outside the executor phase's scope")
        if how == "stub":
            raise NotImplementedStage("HalfWiredWriter.write", 8)
        before = self.cluster.get(request.kind, request.name)
        if how in ("success", "stale", "ambiguous", "raise", "junk"):
            self.cluster.apply(request)
        if how == "success":
            return WriteReply(WriteClass.SUCCESS, self.cluster.get(request.kind, request.name))
        if how in ("stale", "lie"):
            return WriteReply(WriteClass.SUCCESS, before, "ok")
        if how == "invalid":
            return WriteReply(WriteClass.INVALID_ACTION, None, "422 Invalid: spec is invalid")
        if how == "definite":
            return WriteReply(WriteClass.DEFINITE_FAILURE, None, "403 Forbidden")
        if how == "not_attempted":
            return WriteReply(WriteClass.NOT_ATTEMPTED, None, "credential expired")
        if how in ("ambiguous", "lost"):
            return WriteReply(WriteClass.AMBIGUOUS, None, "timed out after 10s")
        if how == "raise":
            raise ConnectionResetError("connection reset after send")
        if how == "junk":
            return "exit 0"
        raise AssertionError(how)


class FakeReader:
    """Reads the cluster. `unavailable` is how many attempts fail before reads succeed
    (an int), or a list with one entry per call: True = unavailable, False = a normal read,
    an exception instance = raise it (after the call is recorded)."""

    def __init__(self, cluster: FakeCluster, unavailable=0) -> None:
        self.cluster = cluster
        self.unavailable = unavailable
        self.calls: list[tuple] = []
        self.replies: list[ReadReply] = []

    def _down(self) -> bool:
        if isinstance(self.unavailable, list):
            down = self.unavailable.pop(0) if self.unavailable else False
            if isinstance(down, Exception):
                raise down
            return down
        if self.unavailable > 0:
            self.unavailable -= 1
            return True
        return False

    def get(self, kind, name, namespace, timeout_s):
        self.calls.append(("get", kind, name, namespace, timeout_s))
        if self._down():
            reply = ReadReply(ReadOutcome.UNAVAILABLE, diagnostic="connection refused")
        else:
            obj = self.cluster.get(kind, name)
            reply = (
                ReadReply(ReadOutcome.ABSENT) if obj is None else ReadReply(ReadOutcome.FOUND, obj)
            )
        self.replies.append(reply)
        return reply

    def list(self, kind, namespace, timeout_s):
        self.calls.append(("list", kind, None, namespace, timeout_s))
        if self._down():
            reply = ReadReply(ReadOutcome.UNAVAILABLE, diagnostic="connection refused")
        else:
            items = [copy.deepcopy(o) for (k, _), o in self.cluster.objects.items() if k == kind]
            reply = ReadReply(ReadOutcome.FOUND, {"items": items})
        self.replies.append(reply)
        return reply


class RecordingAudit:
    """An in-memory ExecutorAudit. Bound to RUN_A unless `run_id=None`. `fail` maps an event
    name (before_write, after_write, read, final) to an exception raised on that event, after
    the attempt is recorded; a list gives one entry per attempt (None = succeed).

    attempts: every (event, fields) append attempted, in order.
    events:   the (event, fields) appends that succeeded (what a file would hold)."""

    def __init__(self, run_id: str | None = RUN_A, fail: dict | None = None) -> None:
        self._run_id = None if run_id is None else canonical_run_id(run_id)
        self.fail = dict(fail or {})
        self.attempts: list[tuple[str, dict]] = []
        self.events: list[tuple[str, str, dict]] = []  # (run_id, event, fields)

    def bind_run(self, run_id: str) -> None:
        self._run_id = canonical_run_id(run_id)

    def require_bound(self) -> None:
        if self._run_id is None:
            raise HarnessMisuse("the recording audit sink is unbound")

    def _append(self, event: str, fields: dict) -> None:
        self.require_bound()
        self.attempts.append((event, fields))
        how = self.fail.get(event)
        if isinstance(how, list):
            how = how.pop(0) if how else None
        if how is not None:
            raise how
        self.events.append((self._run_id, event, fields))

    def before_write(self, request, identity: str) -> None:
        self._append("before_write", {"name": request.name, "identity": identity})

    def after_write(self, request, reply) -> None:
        self._append("after_write", {"name": request.name, "class": reply.semantic_class})

    def read(self, **fields) -> None:
        self._append("read", fields)

    def final(self, result) -> None:
        self._append("final", {"result": result})

    def kinds(self, *, attempted: bool = False) -> list[str]:
        if attempted:
            return [e for e, _ in self.attempts]
        return [e for _, e, _ in self.events]


class Clock:
    """A scripted clock: each call returns the next instant (the last one repeats)."""

    def __init__(self, *instants: datetime) -> None:
        self.instants = list(instants) or [datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)]
        self.calls: list[datetime] = []

    def __call__(self) -> datetime:
        now = self.instants.pop(0) if len(self.instants) > 1 else self.instants[0]
        self.calls.append(now)
        return now


T0 = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


class Rig:
    """One executor over a fake cluster, with every interaction recorded."""

    def __init__(
        self,
        script=(),
        *,
        unavailable=0,
        topology=TOPOLOGY,
        cluster=None,
        clock=None,
        preflight_raises=None,
        namespace=NS,
        audit=None,
    ) -> None:
        self.cluster = cluster or FakeCluster(seed_objects(namespace))
        self.clock = clock or Clock(T0, T0 + timedelta(seconds=1), T0 + timedelta(seconds=2))
        self.writer = FakeWriter(self.cluster, script, preflight_raises, self.clock)
        self.reader = FakeReader(self.cluster, unavailable)
        self.sleeps: list[float] = []
        self.audit = audit if audit is not None else RecordingAudit()
        self.executor = ActionExecutor(
            semantics=SEMANTICS,
            topology=topology,
            namespace=namespace,
            reader=self.reader,
            writer=self.writer,
            audit=self.audit,
            clock=self.clock,
            sleep=self.sleeps.append,
        )

    def apply(self, action):
        return self.executor.apply(action)

    @property
    def untouched(self) -> bool:
        """No read, no preflight, no write."""
        return not self.reader.calls and not self.writer.preflights and not self.writer.writes


def action(type_: str, kind: str, name: str, params: dict, ns: str = NS) -> RecommendedAction:
    return RecommendedAction(
        type=ActionType(type_), target=ActionTarget(kind=kind, ns=ns, name=name), params=params
    )


def restart(name="orders", kind="Deployment", ns=NS):
    return action("restart_deployment", kind, name, {}, ns)


def scale(replicas, name="orders"):
    return action("scale_deployment", "Deployment", name, {"replicas": replicas})


def rollback(revision, name="orders"):
    return action("rollback_deployment", "Deployment", name, {"to_revision": revision})


def patch_configmap(values: dict, name="db-pool-config"):
    return action("patch_configmap", "ConfigMap", name, values)


def patch_service(selector: dict, name="orders-svc"):
    return action("patch_service", "Service", name, {"selector": selector})


def resources(container="orders", name="orders", **sections):
    return action(
        "patch_deployment_resources", "Deployment", name, {"container": container, **sections}
    )
