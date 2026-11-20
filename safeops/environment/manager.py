"""Environment lifecycle, clean-state verification, canonical snapshots, quarantine
(design §11.1; decisions 0012, 0013).

Rule 15: reset, snapshot and quarantine are never faked. Rule 23: a failed cluster is
never hand-patched. A dirty environment is quarantined and rebuilt by the real
cluster-down / cluster-up entry points.

Runtime phase only: every cluster call uses the experiment-controller credential and the
runtime guard (testbed namespace only). Node-level checks are the host-side capability of
decision 0012. Reachable from RUN_SEQUENCE, so the import policy of decision 0004 applies:
nothing heavy is imported at module scope.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

STABILIZATION_TIMEOUT_S = 120  # design §5
STABILIZATION_POLL_S = 2.0
CHAOS_API_GROUP = "chaos-mesh.org"
# Post-fault settledness (decision 0013, Milestone 4 requirement; values: decision 0015).
# Settled = the canonical projection identical across two consecutive polling intervals.
SETTLE_POLL_S = 5.0  # the testbed's readiness probe period, so a pod can flip once per poll
SETTLE_TIMEOUT_S = 60.0
SETTLE_IDENTICAL_POLLS = 3  # three identical polls span two intervals


class EnvironmentStateError(Exception):
    """Base for lifecycle failures."""


class WorkerQuarantined(EnvironmentStateError):
    """A quarantine marker is present; this worker takes no runs until it's rebuilt."""


class ConfigurationError(EnvironmentStateError):
    """SafeOps' own configuration is wrong or stale: desired state, manifests, topology, or
    images that don't match what cluster-up loaded. Never reported as a dirty environment:
    a rebuild can't fix it (Milestone 3 second review, R1, R4)."""


class QuarantineFailed(EnvironmentStateError):
    """The rebuild didn't produce a clean environment. The worker stays halted."""


@dataclass(frozen=True)
class CleanStateResult:
    clean: bool
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettledSnapshot:
    """The post-fault harm baseline and whether the state had settled when it was taken.

    Settled is not healthy: a fault that leaves pods in ImagePullBackOff can be perfectly
    settled. Not settled means the projection was still changing at the timeout; the
    snapshot is then the last poll, and the run is flagged (Milestone 5), never silently
    treated as a clean baseline.
    """

    snapshot: object  # safeops.environment.snapshot.Snapshot
    settled: bool
    polls: int
    elapsed_s: float
    last_changes: tuple[str, ...] = ()  # diff between the last two polls if not settled


# Inventory of every other namespaced kind the testbed could hold (Milestone 3 review, H1).
# The snapshot covers the five projected kinds; this covers the rest, so an object nobody
# expects (a NetworkPolicy that breaks the app, a quota, a bare pod) is never invisible.
# resource -> (kind, reset deletes unexpected ones?). Kinds reset may not delete (pods,
# controller-made objects, RBAC) are flagged by verify, which leads to quarantine.
# Secrets are deliberately absent: no SafeOps identity reads Secrets (residual, 0013).
INVENTORY = {
    "pods": ("Pod", False),
    "replicasets.apps": ("ReplicaSet", False),
    "controllerrevisions.apps": ("ControllerRevision", False),
    "serviceaccounts": ("ServiceAccount", True),
    "networkpolicies.networking.k8s.io": ("NetworkPolicy", True),
    "resourcequotas": ("ResourceQuota", True),
    "limitranges": ("LimitRange", True),
    "horizontalpodautoscalers.autoscaling": ("HorizontalPodAutoscaler", True),
    "poddisruptionbudgets.policy": ("PodDisruptionBudget", True),
    "jobs.batch": ("Job", True),
    "cronjobs.batch": ("CronJob", True),
    "ingresses.networking.k8s.io": ("Ingress", True),
    # second review, R3: inert kinds that create no pods could otherwise hide
    "daemonsets.apps": ("DaemonSet", True),
    "replicationcontrollers": ("ReplicationController", True),
    "podtemplates": ("PodTemplate", True),
    "leases.coordination.k8s.io": ("Lease", True),
    # second review, R5: a hand-made slice can inject endpoints into a Service
    "endpointslices.discovery.k8s.io": ("EndpointSlice", True),
    "roles.rbac.authorization.k8s.io": ("Role", False),
    "rolebindings.rbac.authorization.k8s.io": ("RoleBinding", False),
}


def _controller(obj: dict) -> tuple[str, str] | None:
    ref = next(
        (r for r in obj["metadata"].get("ownerReferences") or [] if r.get("controller")), None
    )
    return (ref["kind"], ref["name"]) if ref else None


def unexpected_objects(
    items: dict[str, list[dict]],
    deployments: set[str],
    statefulsets: set[str],
    rbac: set[tuple[str, str]],
    services: set[str] = frozenset(),
) -> list[tuple[str, str, str, bool]]:
    """(resource, kind, name, deletable_by_reset) for every inventoried object that the
    desired state doesn't account for. Pure, for tests.

    Expected: pods owned (via their ReplicaSet) by a desired Deployment or by a desired
    StatefulSet; ReplicaSets and ControllerRevisions owned by desired workloads; the
    `default` ServiceAccount; the RBAC objects SafeOps itself creates in the testbed; and
    EndpointSlices that the endpointslice controller manages for a desired Service.
    Nothing else of these kinds belongs in the testbed.
    """
    rs_owner = {rs["metadata"]["name"]: _controller(rs) for rs in items.get("replicasets.apps", [])}
    out = []
    for resource, (kind, deletable) in INVENTORY.items():
        for obj in items.get(resource, []):
            name, ctrl = obj["metadata"]["name"], _controller(obj)
            if kind == "Pod":
                if ctrl and ctrl[0] == "ReplicaSet":
                    ctrl = rs_owner.get(ctrl[1])
                ok = ctrl in {("Deployment", d) for d in deployments} | {
                    ("StatefulSet", s) for s in statefulsets
                }
            elif kind == "ReplicaSet":
                ok = ctrl in {("Deployment", d) for d in deployments}
            elif kind == "ControllerRevision":
                ok = ctrl in {("StatefulSet", s) for s in statefulsets}
            elif kind == "ServiceAccount":
                ok = name == "default"
            elif kind == "EndpointSlice":
                ok = is_controller_slice(obj) and ctrl in {("Service", sv) for sv in services}
            elif kind in ("Role", "RoleBinding"):
                ok = (kind, name) in rbac
            else:
                ok = False
            if not ok:
                out.append((resource, kind, name, deletable))
    return out


SLICE_CONTROLLER = "endpointslice-controller.k8s.io"


def is_controller_slice(slice_: dict) -> bool:
    labels = slice_["metadata"].get("labels") or {}
    return labels.get("endpointslice.kubernetes.io/managed-by") == SLICE_CONTROLLER


def _ready(pod: dict) -> bool:
    if pod["metadata"].get("deletionTimestamp"):
        return False
    conds = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)


def _selects(selector: dict, labels: dict) -> bool:
    return bool(selector) and all((labels or {}).get(k) == v for k, v in selector.items())


def stability_problems(deployments, statefulsets, services, slices, pods) -> list[str]:
    """Why the testbed isn't settled yet; empty when it is. Pure, for tests."""
    out = []
    for d in deployments:
        name, spec, st = d["metadata"]["name"], d.get("spec", {}), d.get("status", {})
        want = spec.get("replicas", 1)
        if st.get("observedGeneration", 0) < d["metadata"].get("generation", 0):
            out.append(f"Deployment/{name}: spec change not yet observed")
        for field in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"):
            if st.get(field, 0) != want:
                out.append(f"Deployment/{name}: {field} {st.get(field, 0)} != {want}")
    for s in statefulsets:
        name, spec, st = s["metadata"]["name"], s.get("spec", {}), s.get("status", {})
        want = spec.get("replicas", 1)
        if st.get("observedGeneration", 0) < s["metadata"].get("generation", 0):
            out.append(f"StatefulSet/{name}: spec change not yet observed")
        if st.get("readyReplicas", 0) != want:
            out.append(f"StatefulSet/{name}: readyReplicas {st.get('readyReplicas', 0)} != {want}")
        if st.get("currentRevision") != st.get("updateRevision"):
            out.append(f"StatefulSet/{name}: rollout in progress")
    terminating = [p["metadata"]["name"] for p in pods if p["metadata"].get("deletionTimestamp")]
    if terminating:
        out.append(f"pods still terminating: {', '.join(sorted(terminating))}")
    for svc in services:
        name = svc["metadata"]["name"]
        selector = (svc.get("spec") or {}).get("selector") or {}
        expected = sum(
            1 for p in pods if _ready(p) and _selects(selector, p["metadata"].get("labels"))
        )
        # Only controller-managed slices: a hand-made one is a stray object (the inventory
        # flags it at once), not "not yet stable", so it doesn't stall verify for the whole
        # window (second review, R5). The snapshot still counts every slice.
        got = sum(
            1
            for sl in slices
            if is_controller_slice(sl)
            and (sl["metadata"].get("labels") or {}).get("kubernetes.io/service-name") == name
            for ep in sl.get("endpoints") or []
            if (ep.get("conditions") or {}).get("ready") is True
        )
        if got != expected:
            out.append(f"Service/{name}: {got} ready endpoints, {expected} ready selected pods")
    return out


class EnvironmentManager:
    """Construction has no side effects; everything is fetched lazily."""

    def __init__(
        self,
        kubectl=None,
        node=None,
        rebuild: Callable[[], None] | None = None,
        image_check: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stabilization_timeout_s: float = STABILIZATION_TIMEOUT_S,
        settle_timeout_s: float = SETTLE_TIMEOUT_S,
    ) -> None:
        self._kubectl = kubectl
        self._node = node
        self._injected = (kubectl is not None, node is not None)
        self._rebuild = rebuild
        self._image_check = image_check
        self._clock = clock
        self._sleep = sleep
        self.stabilization_timeout_s = stabilization_timeout_s
        self.settle_timeout_s = settle_timeout_s
        self._cfg = None
        self._topology = None

    # --- wiring --------------------------------------------------------------------------

    @property
    def cfg(self):
        if self._cfg is None:
            from safeops.cluster import load_cluster_config

            self._cfg = load_cluster_config()
        return self._cfg

    @property
    def topology(self):
        if self._topology is None:
            from safeops.topology import load_topology

            self._topology = load_topology()
        return self._topology

    def _k(self):
        if self._kubectl is None:
            from safeops.cluster import Kubectl, Phase

            self._kubectl = Kubectl(self.cfg, Phase.RUNTIME)
        return self._kubectl

    def _nodes(self):
        if self._node is None:
            import yaml

            from safeops.environment.node import NodeInspector

            kc = yaml.safe_load(self.cfg.harness_kubeconfig_path.read_text())
            server = next(
                c["cluster"]["server"] for c in kc["clusters"] if c["name"] == self.cfg.kube_context
            )
            self._node = NodeInspector(self.cfg, expected_api_server=server)
        return self._node

    @property
    def _marker(self):
        return self.cfg.state_dir / "quarantine.json"

    # --- reads ---------------------------------------------------------------------------

    def _items(self, kinds: str) -> list[dict]:
        return self._k().get_json(kinds, "-n", self.cfg.namespace)["items"]

    def _chaos_objects(self) -> list[tuple[str, str, str]]:
        """(resource, kind, name) of every Chaos Mesh object in the testbed namespace."""
        out = self._k().read(
            "api-resources", f"--api-group={CHAOS_API_GROUP}", "--namespaced=true", "--no-headers"
        )
        found = []
        for line in out.splitlines():
            if not line.strip():
                continue
            cols = line.split()
            resource, kind = f"{cols[0]}.{CHAOS_API_GROUP}", cols[-1]
            for obj in self._items(resource):
                found.append((resource, kind, obj["metadata"]["name"]))
        return found

    def _desired_objects(self) -> list[dict]:
        from safeops import manifests

        values = manifests.template_values(self.cfg, manifests.image_refs())
        return [
            o
            for f in manifests.manifest_files("testbed")
            for o in manifests.load_objects(f, values)
        ]

    def _rbac_allowlist(self) -> set[tuple[str, str]]:
        """RBAC objects SafeOps itself creates in the testbed namespace (system manifests)."""
        from safeops import manifests

        values = manifests.template_values(self.cfg, manifests.image_refs())
        return {
            (o["kind"], o["metadata"]["name"])
            for f in manifests.manifest_files("system")
            for o in manifests.load_objects(f, values)
            if o["kind"] in ("Role", "RoleBinding")
            and o["metadata"].get("namespace") == self.cfg.namespace
        }

    def _unexpected(self, desired: list[dict], rbac: set[tuple[str, str]]) -> list:
        items = {resource: self._items(resource) for resource in INVENTORY}
        return unexpected_objects(
            items,
            {o["metadata"]["name"] for o in desired if o["kind"] == "Deployment"},
            {o["metadata"]["name"] for o in desired if o["kind"] == "StatefulSet"},
            rbac,
            {o["metadata"]["name"] for o in desired if o["kind"] == "Service"},
        )

    def _check_images_loaded(self) -> None:
        """The images reset will deploy must be the ones cluster-up loaded into kind.
        Otherwise reset deploys a tag kind doesn't have, ImagePullBackOff, a 120 s timeout,
        a dirty verify and a futile rebuild follow, with no message saying why (R4)."""
        from safeops import manifests

        record = self.cfg.state_dir / "images.json"
        try:
            built = json.loads(record.read_text())["built"]
        except (OSError, ValueError, KeyError) as exc:
            raise ConfigurationError(
                f"no usable record of the images cluster-up loaded ({record.name});"
                " run make cluster-up"
            ) from exc
        for image, ref in manifests.image_refs().items():
            loaded = built.get(image, {}).get("ref")
            if loaded != ref:
                raise ConfigurationError(
                    f"testbed source changed since cluster-up: {image} is now {ref}, but kind has"
                    f" {loaded}; run make cluster-down && make cluster-up"
                )

    def _configuration(self) -> tuple:
        """Everything reset and verify compare against. A failure here is misconfiguration
        (ConfigurationError), never a dirty environment (R1)."""
        from safeops.environment import snapshot
        from safeops.environment.desired import desired_snapshot, load_desired_state

        try:
            desired_state = load_desired_state()
            current = desired_snapshot()  # what the current manifests + topology generate
            desired_objects = self._desired_objects()
            rbac = self._rbac_allowlist()
            _ = self.topology
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError(
                f"SafeOps configuration could not be loaded: {type(exc).__name__}: {exc}"
            ) from exc
        # A valid but stale file would make reset and verify chase an old target (third
        # review). Never rewritten here: regenerating it is a reviewed, committed change.
        stale = snapshot.diff(desired_state, current)
        if stale:
            raise ConfigurationError(
                "config/desired_state.yaml is stale; run make desired-state"
                f" ({len(stale)} differences, e.g. {stale[0]})"
            )
        if self._image_check:
            self._check_images_loaded()
        return desired_state, desired_objects, rbac

    def capture_snapshot(self):
        """Canonical semantic projection of the testbed (§11.1). Never a raw object dump."""
        from safeops.environment import snapshot

        objects = self._items(",".join(k.lower() for k in snapshot.PROJECTED_KINDS))
        slices = self._items("endpointslices.discovery.k8s.io")
        pods = self._items("pods")
        replicasets = self._items("replicasets")
        return snapshot.build(objects, slices, pods, replicasets, self.topology)

    def capture_settled_snapshot(self) -> SettledSnapshot:
        """Post-fault snapshot, taken once the state has settled (decision 0013).

        Polls the canonical projection every SETTLE_POLL_S until SETTLE_IDENTICAL_POLLS
        consecutive polls are identical, or SETTLE_TIMEOUT_S passes. Observed from the
        cluster, never declared per fault. Separate from wait_stabilization_window, which
        waits for health and stays what reset and verify use.
        """
        from safeops.environment import snapshot as snap

        started = self._clock()
        current = self.capture_snapshot()
        polls, identical, changes = 1, 1, ()
        while identical < SETTLE_IDENTICAL_POLLS:
            if self._clock() - started + SETTLE_POLL_S > self.settle_timeout_s:
                return SettledSnapshot(current, False, polls, self._clock() - started, changes)
            self._sleep(SETTLE_POLL_S)
            previous, current = current, self.capture_snapshot()
            polls += 1
            changes = tuple(snap.diff(previous, current))
            if not changes and previous.digest() != current.digest():
                changes = ("canonical digest changed",)  # belt and braces over diff()
            identical = identical + 1 if not changes else 1
        return SettledSnapshot(current, True, polls, self._clock() - started)

    def wait_stabilization_window(self) -> bool:
        """Wait until every deployment's observed generation matches the desired one with
        all replicas ready, and endpoints have caught up, or until 120 s pass (§5).
        Returns False on timeout; the run is flagged, not discarded."""
        deadline = self._clock() + self.stabilization_timeout_s
        while True:
            problems = stability_problems(
                self._items("deployments"),
                self._items("statefulsets"),
                self._items("services"),
                self._items("endpointslices.discovery.k8s.io"),
                self._items("pods"),
            )
            if not problems:
                return True
            if self._clock() >= deadline:
                self.last_stability_problems = problems
                return False
            self._sleep(STABILIZATION_POLL_S)

    # --- reset ---------------------------------------------------------------------------

    def _refuse_if_quarantined(self) -> None:
        if self._marker.is_file():
            raise WorkerQuarantined(f"worker is quarantined ({self._marker.read_text().strip()})")

    def reset_environment(self) -> None:
        """Restore the testbed namespace to config/desired_state.yaml (decision 0013).

        1. Delete every Chaos Mesh object in the testbed, and wait until it's gone.
        2. Delete every projected-kind object the desired state doesn't contain, and every
           unexpected object of the deletable inventory kinds (NetworkPolicies, quotas,
           LimitRanges, HPAs, PDBs, Jobs, CronJobs, Ingresses, extra ServiceAccounts).
           Unexpected pods, ReplicaSets, ControllerRevisions and RBAC objects aren't
           deleted here: verify flags them, which leads to quarantine.
        3. Recreate the app tier (Deployments, Services, ConfigMaps) from the desired
           manifests, so revision history restarts at 1 every run.
        4. Postgres: replace (PUT) the StatefulSet with its desired spec every time. That's
           a no-op when nothing changed, and it removes unprojected drift too. Its PVC and
           data are never touched.
        5. Wait for the stabilization window.

        verify_clean_state() then checks the result independently. Reset never inspects
        or changes node state; leftover node residue is quarantine's job.
        """
        from safeops.environment import snapshot

        self._refuse_if_quarantined()
        _, desired, rbac = self._configuration()  # before anything is deleted
        k, ns = self._k(), self.cfg.namespace

        for resource, kind, name in self._chaos_objects():
            k.delete(resource, kind, name, ns)

        for resource, kind, name, deletable in self._unexpected(desired, rbac):
            if deletable:
                k.delete(resource, kind, name, ns)
        desired_keys = {(o["kind"], o["metadata"]["name"]) for o in desired}
        expected_pvcs = {
            f"{t['metadata']['name']}-{o['metadata']['name']}-{i}"
            for o in desired
            if o["kind"] == "StatefulSet"
            for t in o["spec"].get("volumeClaimTemplates") or []
            for i in range(o["spec"].get("replicas", 1))
        }
        live = self._items(",".join(x.lower() for x in snapshot.PROJECTED_KINDS))
        for obj in live:
            key = (obj["kind"], obj["metadata"]["name"])
            if key in snapshot.IGNORED or key in desired_keys:
                continue
            if key[0] == "PersistentVolumeClaim" and key[1] in expected_pvcs:
                continue
            k.delete(key[0].lower(), key[0], key[1], ns)

        app_tier = [o for o in desired if o["kind"] in ("Deployment", "Service", "ConfigMap")]
        live_keys = {(o["kind"], o["metadata"]["name"]) for o in live}
        for kind in ("Deployment", "Service", "ConfigMap"):
            for obj in app_tier:
                key = (obj["kind"], obj["metadata"]["name"])
                if obj["kind"] == kind and key in live_keys:
                    k.delete(kind.lower(), kind, key[1], ns)
        for kind in ("ConfigMap", "Service", "Deployment"):
            batch = [o for o in app_tier if o["kind"] == kind]
            if batch:
                k.create(batch)

        for obj in (o for o in desired if o["kind"] == "StatefulSet"):
            name = obj["metadata"]["name"]
            current = next(
                (o for o in live if o["kind"] == "StatefulSet" and o["metadata"]["name"] == name),
                None,
            )
            if current is None:
                k.create([obj])
            else:
                k.replace([obj])  # unconditional: unprojected drift goes too (review L5)

        self.wait_stabilization_window()

    # --- verification --------------------------------------------------------------------

    def verify_clean_state(self) -> CleanStateResult:
        """Check the live testbed against desired state, independently of reset.

        Includes node-level residue (decision 0012): a fault-backend side effect left
        behind is dirty even with no Chaos CR present. Any check that can't run counts
        as dirty (fail closed). Guard, credential and configuration errors propagate
        instead: they're misconfiguration, which a rebuild can't fix, not a dirty
        environment.
        """
        from safeops.cluster import CredentialError, GuardError
        from safeops.environment import snapshot
        from safeops.environment.node import NodeCheckError

        desired_state, desired_objects, rbac = self._configuration()  # raises, never "dirty"
        problems: list[str] = []
        try:
            if not self.wait_stabilization_window():
                problems.append(
                    "testbed not stable within the stabilization window: "
                    + "; ".join(getattr(self, "last_stability_problems", [])[:5])
                )
            problems += snapshot.diff(desired_state, self.capture_snapshot())
            problems += [
                f"{kind}/{name} ({resource}) still present"
                for resource, kind, name in self._chaos_objects()
            ]
            problems += [
                f"{kind}/{name}: unexpected object in the testbed"
                for _, kind, name, _ in self._unexpected(desired_objects, rbac)
            ]
        except (GuardError, CredentialError):
            raise
        except Exception as exc:  # fail closed: anything that can't be evaluated is dirty
            problems.append(
                f"cluster state could not be evaluated (fail closed): {type(exc).__name__}: {exc}"
            )
        try:
            problems += self._nodes().residue()
        except (GuardError, CredentialError):
            raise
        except Exception as exc:
            kind = "" if isinstance(exc, NodeCheckError) else f"{type(exc).__name__}: "
            problems.append(f"node-level check could not run (fail closed): {kind}{exc}")
        return CleanStateResult(clean=not problems, problems=tuple(problems))

    # --- quarantine ----------------------------------------------------------------------

    def _default_rebuild(self) -> None:
        """Recreate the cluster through the real entry points (decision 0006): never
        hand-patched, and never a wider guard."""
        for command in ("down", "up"):
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "safeops.bootstrap", command], check=False, timeout=1800
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise QuarantineFailed(
                    f"cluster-{command} did not finish during quarantine rebuild: {exc}"
                ) from exc
            if proc.returncode != 0:
                raise QuarantineFailed(f"cluster-{command} failed during quarantine rebuild")

    def quarantine_and_rebuild(self, reason: str = "") -> None:
        """Stop using this worker, recreate the cluster, then verify desired state (§11.1).

        The marker is written first, so a crash mid-rebuild leaves the worker quarantined.
        It's removed only once the rebuilt environment verifies clean. If it doesn't,
        QuarantineFailed is raised and the worker stays halted.
        """
        self._marker.parent.mkdir(parents=True, exist_ok=True)
        self._marker.write_text(
            json.dumps(
                {
                    "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "reason": reason[:2000],
                }
            )
        )
        try:
            (self._rebuild or self._default_rebuild)()
        except QuarantineFailed:
            raise
        except Exception as exc:  # any rebuild failure halts the worker, marker kept
            raise QuarantineFailed(f"rebuild failed: {type(exc).__name__}: {exc}") from exc
        # The rebuild issued new credentials: drop clients built from the old ones (never
        # ones a caller injected).
        if not self._injected[0]:
            self._kubectl = None
        if not self._injected[1]:
            self._node = None
        result = self.verify_clean_state()
        if not result.clean:
            raise QuarantineFailed(
                "rebuilt environment is still not clean: " + "; ".join(result.problems[:10])
            )
        self._marker.unlink()

    def clear_quarantine(self) -> CleanStateResult:
        """Lift a quarantine after an operator has fixed the cause (review L4). The marker is
        removed only if the environment verifies clean right now."""
        result = self.verify_clean_state()
        if result.clean:
            self._marker.unlink(missing_ok=True)
        return result
