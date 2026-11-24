"""Milestone 6 closure, live (decision 0023, closure amendment): get_logs on a container in
CrashLoopBackOff, on the pinned kubelet. Run with `make cluster-test`.

The fixture is a temporary Deployment (never a bare Pod: reset doesn't delete Pods, so a
leftover would quarantine the worker; a leftover Deployment is removed by reset as an
undesired projected object, and garbage collection takes its ReplicaSet and Pod). It runs the
already-loaded testbed image, prints a unique marker and exits non-zero. Its pods carry
`app: orders`, so get_logs reaches them by label without touching topology.yaml; they never
become Ready, so they take no traffic and add no ready endpoint.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from datetime import UTC, datetime

import pytest

from safeops import manifests
from safeops.cluster import Kubectl, Phase, load_cluster_config
from safeops.tools import get_logs
from safeops.tools.reader import ToolContext

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFEOPS_CLUSTER_TESTS") != "1",
    reason="needs a live cluster (make cluster-test)",
)

CFG = load_cluster_config()
NS = CFG.namespace
NAME = "m6-crashloop-fixture"
READER = [
    "kubectl",
    "--kubeconfig",
    str(CFG.reader_kubeconfig_path),
    "--context",
    CFG.kube_context,
]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)


def _verify_clean() -> bool:
    return _run(["uv", "run", "safeops", "verify"]).stdout.strip().endswith("verify: CLEAN")


def _fixture(marker: str) -> dict:
    labels = {"app": "orders", "safeops-test": "crashloop"}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": NS},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": "crash",
                            "image": manifests.image_refs()["testbed"],  # loaded in kind
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["sh", "-c", f'echo "{marker} start=$(date +%s)"; exit 3'],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits": {"memory": "64Mi"},
                            },
                        }
                    ]
                },
            },
        },
    }


def test_get_logs_serves_the_crashed_instance_of_a_waiting_container_honestly() -> None:
    if not _verify_clean():
        pytest.fail("the testbed is not CLEAN (a run may be active): no fixture created")
    harness = Kubectl(CFG, Phase.RUNTIME)
    marker = f"SAFEOPS-CRASH-MARKER-{uuid.uuid4().hex[:8]}"
    created = datetime.now(UTC)
    try:
        harness.create([_fixture(marker)])

        def status():
            pods = harness.get_json("pods", "-n", NS, "-l", "safeops-test=crashloop")["items"]
            if not pods:
                return None, {}
            return pods[0]["metadata"]["name"], (
                pods[0]["status"].get("containerStatuses") or [{}]
            )[0]

        def waiting_on(cs) -> str | None:
            last = (cs.get("lastState") or {}).get("terminated") or {}
            if cs.get("restartCount", 0) >= 3 and "waiting" in (cs.get("state") or {}) and last:
                return last.get("containerID")
            return None

        deadline = time.monotonic() + 420
        while True:  # a stable waiting state: the same crashed instance before and after
            assert time.monotonic() < deadline, "never observed a stable CrashLoopBackOff"
            pod, cs = status()
            cid = waiting_on(cs)
            if cid is None:
                time.sleep(1)
                continue
            plain = _run(READER + ["logs", pod, "-n", NS, "-c", "crash", "--timestamps"])
            previous = _run(
                READER + ["logs", pod, "-n", NS, "-c", "crash", "--timestamps", "--previous"]
            )
            result = get_logs(
                ToolContext.for_reader(not_before=created), "orders", 10, "debug", 200
            )
            if waiting_on(status()[1]) == cid:
                break
            time.sleep(1)

        last = cs["lastState"]["terminated"]
        assert cs["state"]["waiting"]["reason"] == "CrashLoopBackOff" and last["exitCode"] == 3
        started = int(datetime.fromisoformat(last["startedAt"].replace("Z", "+00:00")).timestamp())
        # The pinned kubelet, while waiting: plain logs serve the last terminated instance,
        # and --previous serves the very same one.
        assert plain.returncode == 0 and previous.returncode == 0, (plain.stderr, previous.stderr)
        assert plain.stdout == previous.stdout
        assert f"{marker} start={started}" in plain.stdout
        # SafeOps, through safeops-reader: one read, the crash evidence, and honesty about the
        # earlier crashes inside the window that the kubelet no longer keeps.
        mine = [x for x in result["lines"] if x["pod"] == pod]
        assert [(x["instance"], x["text"]) for x in mine] == [
            ("latest", f"{marker} start={started}")
        ]
        assert result["scan_limited"] is True and result["unavailable"] == []
        # (Ordinary log reads are covered by tests/cluster/test_tools_live.py: with no load
        # running, idle orders pods write nothing after the fixture's floor.)
    finally:
        _run(
            [
                "kubectl",
                "--kubeconfig",
                str(CFG.harness_kubeconfig_path),
                "--context",
                CFG.kube_context,
                "delete",
                "deployment",
                NAME,
                "-n",
                NS,
                "--ignore-not-found",
                "--wait=true",
            ]
        )
        reset = _run(["uv", "run", "safeops", "reset"])
        assert reset.returncode == 0, reset.stdout + reset.stderr
        assert _verify_clean(), "the testbed is not CLEAN after the CrashLoop fixture"


def test_the_no_previous_answer_is_the_form_get_logs_recognizes() -> None:
    from safeops.tools.kube import _NO_PREVIOUS

    harness = Kubectl(CFG, Phase.RUNTIME)
    pods = harness.get_json("pods", "-n", NS, "-l", "app=orders,!safeops-test")["items"]
    # The precondition, proven rather than assumed: a container that has never restarted
    # and has no terminated last state, so the kubelet has no previous instance to serve.
    candidates = [
        (p["metadata"]["name"], cs["name"])
        for p in pods
        for cs in (p.get("status") or {}).get("containerStatuses") or []
        if cs.get("restartCount") == 0
        and "running" in (cs.get("state") or {})
        and not (cs.get("lastState") or {}).get("terminated")
    ]
    if not candidates:
        pytest.fail("precondition: no running orders container with restartCount 0 to test")
    pod, container = sorted(candidates)[0]
    # Through the production path (the reader client's raw read): the CommandError carries the
    # kubelet's last stderr line, which is what get_logs matches. Raw stderr ends in "\n",
    # which the end-anchored pattern deliberately doesn't accept.
    from safeops.cluster import CommandError

    with pytest.raises(CommandError) as exc:
        Kubectl(CFG, Phase.READER).read_raw(
            "logs", pod, "-n", NS, "-c", container, "--previous", "--timestamps"
        )
    assert _NO_PREVIOUS.search(str(exc.value)), str(exc.value)
    assert f'previous terminated container "{container}" in pod "{pod}"' in str(exc.value)
    raw = _run(READER + ["logs", pod, "-n", NS, "-c", container, "--previous"])  # unwrapped
    assert raw.returncode != 0 and _NO_PREVIOUS.search(raw.stderr.strip()), raw.stderr
