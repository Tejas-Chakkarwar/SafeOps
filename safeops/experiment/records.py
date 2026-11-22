"""Run record writer (design §11.8; decisions 0017 §1, §7 and 0018).

One JSON file per attempted run, at data/runs/<run_id>.json, validated against its
schema version before it is written and written exactly once:

- A record is built from the runner's runtime objects, serialized, and validated
  (safeops.experiment.schema). Only a valid record enters data/runs/.
- An invalid record is a writer bug. It is kept under data/runs/rejected/ for diagnosis,
  never in the dataset, and RecordValidationError is raised. A run is never silently lost.
- Files are created exclusively (a hard link from a fully written temporary file), made
  read-only, and never overwritten: finalized records are immutable observations.

Objects from later milestones (trajectory steps, the parsed proposal, the feature vector,
gate and shadow scores, the execution result, the effectiveness and harm results) supply
their record form through a ``to_record()`` method, or are plain dicts already.

Import policy (decision 0004): reachable from RUN_SEQUENCE, so the schema (Pydantic),
safeops.cluster and the environment modules are imported inside methods.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from safeops.enums import RunStage

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNS_DIR = REPO_ROOT / "data" / "runs"
MAX_LAST_CHANGES = 50  # bound the settledness diff kept in a record
MAX_DISCARD_ERROR = 2000  # bound the error text kept in a discard reason


class RecordWriteError(Exception):
    """A record couldn't be written (it would overwrite one, or the disk refused)."""


@dataclass(frozen=True)
class Discard:
    """Why a run was discarded: the stage it stopped at, the cause, and the error.

    The stage must be a RunStage: a misspelled or unknown stage fails here, at once, never
    later as a record that silently took the wrong cleanup path (M5 review, item 2)."""

    stage: RunStage
    cause: str
    error: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", RunStage(self.stage))  # ValueError if unknown

    @property
    def phase(self) -> str:
        from safeops.experiment.schema import stage_phase

        return stage_phase(self.stage)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _known_total(steps: list[dict] | None, key: str) -> int | None:
    """The sum over steps, or None if any step's count is unknown: a partial sum would
    pass for a complete one and undercount cost (M5 review, item 6). Zero stays zero."""
    if steps is None:
        return None
    values = [s.get(key) for s in steps]
    if any(v is None for v in values):
        return None
    return sum(values)


def _plain(obj):
    """A later milestone's object in record form."""
    if obj is None or isinstance(obj, dict | list | str | int | float | bool):
        return obj
    to_record = getattr(obj, "to_record", None)
    if callable(to_record):
        return to_record()
    raise TypeError(f"{type(obj).__name__} has no record form (to_record())")


class ProvenanceSource:
    """Run-independent provenance (§11.8). Cluster facts are best effort: a version that
    can't be read is recorded as null, never guessed. Construction has no side effects."""

    def __init__(
        self,
        repo_root: Path = REPO_ROOT,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        kubectl=None,
        is_pilot_run: bool = True,
    ) -> None:
        self._root = repo_root
        self._run = runner
        self._kubectl = kubectl
        # Every run before the Milestone 14 freeze is a pilot run, excluded from the final
        # evaluation (§10.8). The campaign runner turns this off after the freeze.
        self.is_pilot_run = is_pilot_run

    def _cmd(self, *args: str) -> str | None:
        try:
            proc = self._run(
                list(args), capture_output=True, text=True, timeout=30, check=False, cwd=self._root
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def _kubernetes_version(self) -> str | None:
        try:
            if self._kubectl is None:
                from safeops.cluster import Kubectl, Phase, load_cluster_config

                self._kubectl = Kubectl(load_cluster_config(), Phase.RUNTIME)
            out = self._kubectl.read("version", "-o", "json")
            return json.loads(out)["serverVersion"]["gitVersion"]
        except Exception:
            return None

    def _images(self) -> tuple[dict | None, dict | None, str | None]:
        """Bootstrap-time image provenance (decision 0021), read back from what cluster-up
        captured in .safeops/images.json. `image_digests` are the content-addressed images
        built and loaded at bootstrap; `running_images` are the image identities observed
        on the pods right after bootstrap. Both are frozen at bootstrap and only referenced
        by each record; neither is re-sampled per run. This file is a per-bootstrap
        observation that rebuilds rewrite. The frozen reference is a separate campaign image
        lock under config/, checked at bootstrap/rebuild and at run admission (0022 §3)."""
        try:
            record = json.loads((self._root / ".safeops" / "images.json").read_text())
            built = record["built"]
            digests = {name: info["index_digest"] for name, info in built.items()}
            testbed = built.get("testbed", {}).get("ref")
            return digests, record.get("running"), testbed
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None, None, None

    def collect(self) -> dict:
        from safeops.environment.snapshot import SNAPSHOT_PROJECTION_VERSION

        commit = (self._cmd("git", "rev-parse", "HEAD") or "").strip()
        status = self._cmd("git", "status", "--porcelain")
        kind = self._cmd("kind", "version")
        digests, running, testbed = self._images()
        return {
            "provider": None,
            "model_id": None,
            "model_parameters": None,
            "prompt_hash": None,
            "tool_schema_hash": None,
            "action_semantics_hash": None,
            "gate_feature_version": None,
            "evidence_requirements_hash": None,
            "conflict_rules_hash": None,
            "fault_definition_hash": None,
            "fault_backend": None,
            "desired_state_hash": _sha256_file(self._root / "config" / "desired_state.yaml"),
            "topology_hash": _sha256_file(self._root / "config" / "topology.yaml"),
            "snapshot_projection_version": SNAPSHOT_PROJECTION_VERSION,
            "safeops_commit": commit,
            "safeops_tree_dirty": status is None or bool(status.strip()),
            "kubernetes_version": self._kubernetes_version(),
            "kind_version": kind.split()[1] if kind and len(kind.split()) > 1 else None,
            "testbed_version": testbed,
            "image_digests": digests,
            "running_images": running,
            "is_pilot_run": self.is_pilot_run,
        }


RECORD_FIELDS = frozenset(
    {
        "run_id",
        "unit",
        "started_at",
        "wall_seconds",
        "provenance",
        "clean_state_verified",
        "steady_state",
        "prefault",
        "fault",
        "fault_verification",
        "postfault",
        "alert",
        "trajectory",
        "proposal",
        "features",
        "shadow_scores",
        "decision",
        "action_attempted",
        "execution",
        "stabilized",
        "postaction",
        "effective",
        "harm",
        "reset_verified",
        "discard",
    }
)


class RunRecordWriter:
    """Builds, validates and writes one record per attempted run."""

    def __init__(
        self,
        runs_dir: Path | None = None,  # None: RUNS_DIR, read when the writer is built
        provenance: ProvenanceSource | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        phase: str = "A",
        faults_dir: Path | None = None,
    ) -> None:
        self.runs_dir = Path(RUNS_DIR if runs_dir is None else runs_dir)
        # The same spec source as the injector (build_runner wires both), so a run that
        # never got to injection still records the hash of the spec it would have used.
        self.faults_dir = faults_dir
        self._provenance = provenance
        self._clock = clock
        self.phase = phase

    @property
    def provenance(self) -> ProvenanceSource:
        if self._provenance is None:
            self._provenance = ProvenanceSource()
        return self._provenance

    def capture_provenance(self, unit) -> dict:
        """Provenance as of the start of the run, when the code that runs is the code
        recorded (review finding 3). The runner passes it back to finalize()."""
        from safeops.faults.spec import FAULTS_DIR, fault_path

        provenance = self.provenance.collect()
        path = fault_path(unit.fault_id, self.faults_dir or FAULTS_DIR)
        if path.is_file():
            provenance["fault_definition_hash"] = _sha256_file(path)
        return provenance

    def build(self, **fields) -> dict:
        """The record as a plain dict, not yet validated."""
        unknown = set(fields) - RECORD_FIELDS
        if unknown:
            raise TypeError(f"finalize() got unknown fields {sorted(unknown)}")
        f = {name: fields.get(name) for name in RECORD_FIELDS}
        unit = f["unit"]

        if not isinstance(f["run_id"], str) or not f["run_id"]:
            raise TypeError("finalize() needs the run_id allocated at the start of the run")
        if f["provenance"] is None:
            raise TypeError("finalize() needs the provenance captured at the start of the run")
        # Both are required run state, captured by the runner: the writer never invents a
        # start time, and never derives elapsed time from wall clocks (closeout items 1b, 4).
        started_at = f["started_at"]
        if started_at is None:
            raise TypeError("run record requires started_at captured at run start")
        wall_seconds = f["wall_seconds"]
        if not isinstance(wall_seconds, int | float) or isinstance(wall_seconds, bool):
            raise TypeError("run record requires wall_seconds measured on a monotonic clock")
        if wall_seconds < 0:
            raise ValueError(
                f"monotonic elapsed time is negative ({wall_seconds}s): the clock source is broken"
            )
        finalized_at = self._clock()
        provenance = dict(f["provenance"])
        fault = f["fault"]
        if fault is not None:
            # The spec that was actually injected is authoritative.
            provenance["fault_definition_hash"] = fault.spec.definition_sha256
            provenance["fault_backend"] = dataclasses.asdict(fault.backend)

        verification = f["fault_verification"]
        postfault = f["postfault"]
        proposal = _plain(f["proposal"]) or {}
        trajectory = f["trajectory"]
        steps = None if trajectory is None else [_plain(s) for s in trajectory]
        execution = _plain(f["execution"])
        discard: Discard | None = f["discard"]
        proposal_status = proposal.get("proposal_status")

        in_g = (
            discard is None
            and proposal_status == "valid_action"
            and execution is not None
            and execution.get("status") == "applied"
        )
        outcome = None
        if discard is None and (f["effective"] is not None or f["harm"] is not None):
            from safeops.experiment.schema import category_for

            eff = _plain(f["effective"])
            eff = eff if isinstance(eff, dict) else {"effective": eff, "time_to_recovery_s": None}
            harm = _plain(f["harm"]) or {}
            effective, harmful = eff.get("effective"), harm.get("harmful")
            outcome = {
                "effective": effective,
                "harmful": harmful,
                "harm_violations": harm.get("harm_violations"),
                "time_to_recovery_s": eff.get("time_to_recovery_s"),
                "category": (
                    category_for(effective, harmful)
                    if in_g and effective is not None and harmful is not None
                    else None
                ),
            }

        tokens_in, tokens_out = _known_total(steps, "tokens_in"), _known_total(steps, "tokens_out")
        stabilized = f["stabilized"]

        return {
            # Allocated by the runner at run start and never regenerated here (item 1).
            "run_id": f["run_id"],
            "schema_version": 1,
            "phase": self.phase,
            "unit": {
                "fault_id": unit.fault_id,
                "fault_seed": unit.fault_seed,
                "trial_index": unit.trial_index,
            },
            "started_at": _iso(started_at),
            "finalized_at": _iso(finalized_at),
            "provenance": provenance,
            "clean_state_verified": f["clean_state_verified"],
            "steady_state": (
                None if f["steady_state"] is None else dataclasses.asdict(f["steady_state"])
            ),
            "prefault_snapshot_hash": None if f["prefault"] is None else f["prefault"].digest(),
            "fault": None
            if fault is None
            else {
                "variant_index": fault.variant_index,
                "variant": dataclasses.asdict(fault.variant),
                "injected_at": _iso(fault.injected_at),
                "backend": dataclasses.asdict(fault.backend),
            },
            "fault_verified": None if verification is None else verification.present,
            "fault_verification": None
            if verification is None
            else {
                "present": verification.present,
                "symptom": dataclasses.asdict(verification.symptom),
                "observations": [
                    {
                        "window_id": o.window_id,
                        "attempts": o.attempts,
                        "server_error_responses": o.server_error_responses,
                        "value": o.value,
                        "held": o.held,
                        "observed_at": _iso(o.observed_at),
                    }
                    for o in verification.observations
                ],
                "reason": verification.reason,
            },
            "postfault_snapshot_hash": None if postfault is None else postfault.snapshot.digest(),
            "postfault_settledness": None
            if postfault is None
            else {
                "settled": postfault.settled,
                "polls": postfault.polls,
                "elapsed_s": float(postfault.elapsed_s),
                "last_changes": list(postfault.last_changes)[:MAX_LAST_CHANGES],
            },
            "alert": None if f["alert"] is None else f["alert"].to_dict(),
            "trajectory": steps,
            "proposal_status": proposal_status,
            "proposal": proposal.get("proposal"),
            "feature_vector": _plain(f["features"]),
            "gate": _plain(f["decision"]),
            "shadow_gates": _plain(f["shadow_scores"]),
            "action_attempted": bool(f["action_attempted"]),
            "execution": execution,
            "stabilization_timed_out": None if stabilized is None else not stabilized,
            "postaction_snapshot_hash": None
            if f["postaction"] is None
            else f["postaction"].digest(),
            "outcome": outcome,
            "totals": {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "agent_seconds": None,
                "wall_seconds": float(wall_seconds),
            },
            "reset_verified": f["reset_verified"],
            "discarded": discard is not None,
            "discard_reason": None
            if discard is None
            else {
                "stage": discard.stage,
                "phase": discard.phase,
                "cause": discard.cause,
                "error": discard.error[:MAX_DISCARD_ERROR],
            },
            "in_cohort_g": in_g,
        }

    def finalize(self, **fields) -> Path:
        """Validate the run record against its schema version and write it once.

        Finalized records are immutable. Returns the record's path.
        """
        from safeops.experiment.schema import RecordValidationError, validate_record_json

        record = self.build(**fields)
        try:
            # allow_nan=False: NaN and Infinity aren't JSON; refuse them (review finding 6).
            text = json.dumps(record, sort_keys=True, indent=2, allow_nan=False) + "\n"
            validate_record_json(text)
        except (ValueError, RecordValidationError) as exc:
            text = json.dumps(record, sort_keys=True, indent=2) + "\n"
            kept = self._write_once(self.runs_dir / "rejected", record["run_id"], text)
            raise RecordValidationError(
                f"run record failed validation (writer bug); kept at {kept}: {exc}"
            ) from exc
        return self._write_once(self.runs_dir, record["run_id"], text)

    @staticmethod
    def _write_once(directory: Path, run_id: str, text: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"{run_id}.json"
        tmp = directory / f".{run_id}.json.tmp"
        installed = False
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
                fh.flush()
                _durable_sync(fh.fileno())
            os.link(tmp, final)  # fails if the record exists: never overwritten
            installed = True
            # The link is a directory entry: sync the directory too, or a crash during the
            # destructive recovery that follows could lose a record finalize reported as
            # written (M5 second review, finding 5).
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                _durable_sync(dir_fd)
            finally:
                os.close(dir_fd)
        except FileExistsError as exc:
            raise RecordWriteError(f"run record {final} already exists; never overwritten") from exc
        except OSError as exc:
            if installed or final.exists():
                # The record is on disk; only the durability confirmation failed. Say so,
                # and leave it in place, read-only (M5 review, item 9).
                try:
                    os.chmod(final, 0o444)
                except OSError:
                    pass
                raise RecordWriteError(
                    f"run record {final} is installed, but confirming it durable failed: {exc}"
                ) from exc
            raise RecordWriteError(f"run record {final} was not installed: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)
        os.chmod(final, 0o444)
        return final


def _durable_sync(fd: int) -> None:
    """fsync, plus F_FULLFSYNC where it exists: on macOS plain fsync leaves data in the
    drive's cache, so it isn't durable across a power loss."""
    os.fsync(fd)
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
        except OSError:
            pass  # not supported on this file system; the fsync above still happened


def load_record(path: Path):
    """Read and validate a finalized record (against exactly its declared version)."""
    from safeops.experiment.schema import validate_record_json

    record = validate_record_json(Path(path).read_text())
    if Path(path).stem != record.run_id:  # the filename is derived from run_id too
        from safeops.experiment.schema import RecordValidationError

        raise RecordValidationError(f"{Path(path).name} holds run {record.run_id}")
    return record
