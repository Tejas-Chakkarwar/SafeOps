"""Structural action contracts (decision 0025 D§3.4): what a recommended action must look
like to *mean* something. The one definition, shared by the proposal parser (Milestone 8A)
and the executor (Milestone 8B).

Structural only. Nothing here knows the testbed: no namespace, no ownership map, no hard or
safe range, no revision history, no container list. A well-typed action whose values the
hard envelope forbids (replicas 500, a StatefulSet target, an unknown Deployment) is
structurally valid here and is refused later by the executor, so that Layer 1 refusals stay
reachable (design §8; decision 0025 D§3.4).

Neutral (decision 0020): stdlib and safeops.enums only. tests/test_information_wall.py
checks it with the protected packages.
"""

from __future__ import annotations

import math
import re

from safeops.enums import ActionType

ACTION_KEYS = frozenset({"type", "target", "params"})
TARGET_KEYS = frozenset({"kind", "ns", "name"})
RESOURCE_SECTIONS = ("requests", "limits")
RESOURCE_FIELDS = frozenset({"cpu_millis", "memory_mib"})
MAX_CONFIGMAP_PATCH_KEYS = 64  # the get_configmap bound (decision 0023 §6)
MAX_SELECTOR_ENTRIES = 32  # the get_service selector bound (decision 0023 §6)

KIND = re.compile(r"[A-Z][A-Za-z0-9]{0,62}")
DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_SUBDOMAIN_PART = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
DNS_SUBDOMAIN = re.compile(rf"{_SUBDOMAIN_PART}(\.{_SUBDOMAIN_PART})*")
CONFIGMAP_KEY = re.compile(r"[-._a-zA-Z0-9]{1,253}")
_LABEL_NAME = re.compile(r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")


def exact_int(value: object) -> bool:
    """A JSON integer: an int that is not a bool (bool subclasses int)."""
    return type(value) is int


def is_dns_label(value: object) -> bool:
    return type(value) is str and DNS_LABEL.fullmatch(value) is not None


def is_dns_subdomain(value: object) -> bool:
    return type(value) is str and len(value) <= 253 and DNS_SUBDOMAIN.fullmatch(value) is not None


def is_label_key(value: object) -> bool:
    """Kubernetes label key: an optional DNS-subdomain prefix and '/', then a name of 1..63."""
    if type(value) is not str:
        return False
    prefix, sep, name = value.rpartition("/")
    if sep and not is_dns_subdomain(prefix):
        return False
    return len(name) <= 63 and _LABEL_NAME.fullmatch(name) is not None


def is_label_value(value: object) -> bool:
    """Kubernetes label value: empty, or a name of 1..63."""
    if type(value) is not str:
        return False
    return value == "" or (len(value) <= 63 and _LABEL_NAME.fullmatch(value) is not None)


def target_problem(target: object) -> str | None:
    """Why `target` is not a structurally valid {kind, ns, name}, or None."""
    if type(target) is not dict or set(target) != TARGET_KEYS:
        return f"target must be an object with exactly {sorted(TARGET_KEYS)}"
    if type(target["kind"]) is not str or KIND.fullmatch(target["kind"]) is None:
        return "target.kind is not a Kubernetes kind"
    if not is_dns_label(target["ns"]):
        return "target.ns is not a DNS-1123 label"
    if not is_dns_subdomain(target["name"]):
        return "target.name is not a DNS-1123 subdomain"
    return None


def _configmap_value_ok(value: object) -> bool:
    if type(value) is str or exact_int(value):
        return True
    return type(value) is float and math.isfinite(value)


def _resources_problem(params: dict) -> str | None:
    if set(params) - {"container", *RESOURCE_SECTIONS} or "container" not in params:
        return "params must be {container, requests?, limits?}"
    if not is_dns_label(params["container"]):
        return "params.container is not a DNS-1123 label"
    present = [s for s in RESOURCE_SECTIONS if s in params]
    if not present:
        return "params needs requests or limits"
    for section in present:
        fields = params[section]
        if type(fields) is not dict or not fields:
            return f"params.{section} must be a non-empty object"
        if set(fields) - RESOURCE_FIELDS:
            return f"params.{section} may hold only {sorted(RESOURCE_FIELDS)}"
        if not all(exact_int(v) for v in fields.values()):
            return f"params.{section} values must be integers"
    return None


def params_problem(action_type: ActionType, params: object) -> str | None:
    """Why `params` is not the structural contract of `action_type`, or None. Types and
    shapes only: no range, membership or existence check (those are the hard envelope's)."""
    if type(params) is not dict:
        return "params must be an object"
    if action_type is ActionType.ROLLBACK_DEPLOYMENT:
        if set(params) != {"to_revision"}:
            return "params must be exactly {to_revision}"
        value = params["to_revision"]
        if not (exact_int(value) and value >= 1):
            return "params.to_revision must be an integer >= 1"
        return None
    if action_type is ActionType.RESTART_DEPLOYMENT:
        return None if params == {} else "params must be empty"
    if action_type is ActionType.SCALE_DEPLOYMENT:
        if set(params) != {"replicas"}:
            return "params must be exactly {replicas}"
        if not exact_int(params["replicas"]):
            return "params.replicas must be an integer"
        return None
    if action_type is ActionType.PATCH_CONFIGMAP:
        if not params or len(params) > MAX_CONFIGMAP_PATCH_KEYS:
            return f"params must hold 1..{MAX_CONFIGMAP_PATCH_KEYS} keys"
        for key, value in params.items():
            if CONFIGMAP_KEY.fullmatch(key) is None:
                return "a params key is not a ConfigMap key"
            if not _configmap_value_ok(value):
                return f"params.{key} must be a string, an integer or a finite number"
        return None
    if action_type is ActionType.PATCH_SERVICE:
        if set(params) != {"selector"}:
            return "params must be exactly {selector}"
        selector = params["selector"]
        if type(selector) is not dict or not 1 <= len(selector) <= MAX_SELECTOR_ENTRIES:
            return f"params.selector must hold 1..{MAX_SELECTOR_ENTRIES} labels"
        for key, value in selector.items():
            if not (is_label_key(key) and is_label_value(value)):
                return "params.selector holds a malformed label"
        return None
    if action_type is ActionType.PATCH_DEPLOYMENT_RESOURCES:
        return _resources_problem(params)
    raise AssertionError(f"no structural contract for {action_type!r}")  # a new ActionType
