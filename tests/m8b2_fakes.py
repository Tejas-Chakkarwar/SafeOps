"""Offline doubles for the M8B-2 concrete transports (decision 0025, M8B-2 amendment).

`ProcessCluster` stands in for the two processes the transports run, `docker port` (the
identity check) and `kubectl`, over a FakeCluster: a patch is applied the way the API server
would and answered with the object on stdout and kubectl's -v=6 response line on stderr; a
reader `get` answers with the object, or nothing (--ignore-not-found). Each patch takes the
next scripted outcome. Every argv is recorded, so a test can prove what ran, and in what
order relative to the audit file.

`credentials(cfg, ...)` writes a scratch executor (and reader) kubeconfig whose context's
API server is the port `docker port` reports.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from safeops.executor.transport import PatchType
from tests.conftest import scratch_cluster_config
from tests.m8_executor_fakes import FakeCluster, seed_objects

PORT = 43210
SERVER = f"https://127.0.0.1:{PORT}"
FAR = 2**40  # an expiry far in the future
PLURAL = {"deployments.apps": "deployments", "services": "services", "configmaps": "configmaps"}
API = {"deployments.apps": "/apis/apps/v1", "services": "/api/v1", "configmaps": "/api/v1"}
KIND = {"deployments.apps": "Deployment", "services": "Service", "configmaps": "ConfigMap"}


def scratch_cfg(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return scratch_cluster_config(state)


def _kubeconfig(server: str = SERVER) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "kind-safeops", "cluster": {"server": server}}],
            "users": [{"name": "safeops-executor", "user": {"token": "not-a-real-token"}}],
            "contexts": [
                {
                    "name": "kind-safeops",
                    "context": {"cluster": "kind-safeops", "user": "safeops-executor"},
                }
            ],
        }
    )


def credentials(cfg, *, executor: str = "valid", reader: bool = True, server: str = SERVER):
    """executor: valid | expired | missing | corrupt (exists, but no usable context)."""
    if executor != "missing":
        text = "this is not a kubeconfig" if executor == "corrupt" else _kubeconfig(server)
        cfg.executor_kubeconfig_path.write_text(text)
        expires = 1 if executor == "expired" else FAR
        cfg.executor_token_meta_path.write_text(json.dumps({"expires_at": expires}))
    if reader:
        cfg.reader_kubeconfig_path.write_text(_kubeconfig(server))
        cfg.reader_token_meta_path.write_text(json.dumps({"expires_at": FAR}))
    return cfg


def response_line(method: str, url: str, status: int, *, structured: bool = True) -> str:
    text = {200: "OK", 403: "Forbidden", 422: "Unprocessable Entity", 500: "Internal Server Error"}
    if structured:
        return (
            f'I1126 12:00:00.000000   4242 round_trippers.go:632] "Response" verb="{method}"'
            f' url="{url}" status="{status} {text.get(status, "")}" milliseconds=5'
        )
    return f"I1126 12:00:00.000000   4242 round_trippers.go:553] {method} {url} {status} in 5 ms"


@dataclass
class _Patch:
    kind: str
    name: str
    patch_type: PatchType
    body_json: str

    def body(self):
        return json.loads(self.body_json)


class ProcessCluster:
    """A subprocess.run double: `docker port` and the transports' kubectl commands.

    Patch outcomes (one per patch, default "success"):
      success   applied; 200 with the object
      invalid   not applied; 422 and kubectl's "is invalid" text
      forbidden not applied; 403
      server    applied, then 500
      timeout   applied, then the process timed out
      spawn     kubectl never started (FileNotFoundError)
      raise     applied, then the runner raised something unexpected
    """

    def __init__(self, cluster: FakeCluster | None = None, script=(), *, docker_port=PORT):
        self.cluster = cluster or FakeCluster(seed_objects())
        self.script = list(script)
        self.docker_port = docker_port
        self.calls: list[list[str]] = []
        self.on_patch = None  # called with the argv just before a patch "runs"

    @property
    def patches(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "kubectl" and "patch" in c]

    @property
    def gets(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "kubectl" and "get" in c]

    def __call__(self, args, input=None, capture_output=True, text=True, timeout=None, check=False):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["docker", "port"]:
            return subprocess.CompletedProcess(args, 0, f"127.0.0.1:{self.docker_port}\n", "")
        assert args[0] == "kubectl" and args[1] == "--kubeconfig" and args[3] == "--context"
        verb = args[5]
        if verb == "patch":
            return self._patch(args, text)
        if verb == "get":
            return self._get(args)
        raise AssertionError(f"unexpected kubectl command {args[5:]}")

    def _patch(self, args, text):
        if self.on_patch is not None:
            self.on_patch(args)
        resource, name = args[6], args[7]
        ns = args[args.index("-n") + 1]
        patch_type = PatchType(args[args.index("--type") + 1])
        body = args[args.index("-p") + 1]
        how = self.script.pop(0) if self.script else "success"
        if how == "spawn":
            raise FileNotFoundError("kubectl")
        url = f"{SERVER}{API[resource]}/namespaces/{ns}/{PLURAL[resource]}/{name}?fieldManager=x"
        req = _Patch(KIND[resource], name, patch_type, body)
        if how in ("success", "server", "timeout", "raise"):
            self.cluster.apply(req)
        if how == "timeout":
            raise subprocess.TimeoutExpired(args, 25, output=b"", stderr=b"")
        if how == "raise":
            raise RuntimeError("the runner broke after the request")
        status = {"success": 200, "invalid": 422, "forbidden": 403, "server": 500}[how]
        stderr = response_line("GET", f"{SERVER}/api?timeout=32s", 200) + "\n"
        stderr += response_line("PATCH", url, status) + "\n"
        stdout = ""
        if how == "success":
            stdout = json.dumps(self.cluster.get(KIND[resource], name))
        elif how == "invalid":
            stderr += f'The {KIND[resource]} "{name}" is invalid: spec: Invalid value\n'
        else:
            stderr += f"Error from server ({'Forbidden' if status == 403 else 'InternalError'})\n"
        rc = 0 if status == 200 else 1
        if not text:
            return subprocess.CompletedProcess(args, rc, stdout.encode(), stderr.encode())
        return subprocess.CompletedProcess(args, rc, stdout, stderr)

    def _get(self, args):
        resource = args[6]
        if resource == "replicasets.apps":
            items = [o for (k, _), o in self.cluster.objects.items() if k == "ReplicaSet"]
            out = {"apiVersion": "v1", "kind": "List", "items": items, "metadata": {}}
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        obj = self.cluster.get(KIND[resource], args[7])
        return subprocess.CompletedProcess(args, 0, "" if obj is None else json.dumps(obj), "")
