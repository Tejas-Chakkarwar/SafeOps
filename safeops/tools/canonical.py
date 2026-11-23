"""JSON-safe normalization, canonical serialization and `result_digest` (decision 0023).

Pipeline, in this order, for every tool result:

    raw observation -> projection (only the contract's fields) -> deterministic order
    -> bounding / truncation -> normalize() -> canonical_json() -> result_digest()

The digest is taken over exactly the value the agent is shown and the record stores, after
bounding and normalization, never over an earlier representation.

Non-finite numbers (decision 0018 carry-forward). Strict JSON has no NaN or infinities, and
the run record refuses them. A tool result never contains one: each becomes the tagged
object {"non_finite": "NaN" | "+Inf" | "-Inf"}. It is strict JSON, deterministic, and
distinguishable from null, from zero and from every finite number, so a later reader
(feature extraction) can tell "undefined" from "measured 0". Never 0, never dropped.

Neutral: standard library only.
"""

from __future__ import annotations

import hashlib
import json
import math

NON_FINITE_KEY = "non_finite"
NON_FINITE_VALUES = ("NaN", "+Inf", "-Inf")


def non_finite(value: float) -> dict[str, str]:
    """The tagged representation of a non-finite float."""
    if math.isnan(value):
        return {NON_FINITE_KEY: "NaN"}
    if math.isinf(value):
        return {NON_FINITE_KEY: "+Inf" if value > 0 else "-Inf"}
    raise ValueError(f"{value!r} is finite")


def is_non_finite_tag(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {NON_FINITE_KEY}
        and value[NON_FINITE_KEY] in NON_FINITE_VALUES
    )


def normalize(value):
    """A JSON-safe copy of `value`: every non-finite float, at any depth, becomes its tag.

    Only JSON types pass: dict (string keys), list or tuple (as list), str, int, bool,
    None, and float. Anything else is a bug in the tool that produced it.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else non_finite(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"a tool result key must be a string, got {type(key).__name__}")
            out[key] = normalize(item)
        return out
    if isinstance(value, list | tuple):
        return [normalize(item) for item in value]
    raise TypeError(f"{type(value).__name__} is not a JSON value")


def canonical_json(value) -> str:
    """The one serialization `result_digest` is taken over: keys sorted at every level, no
    whitespace, ASCII only (non-ASCII as \\u escapes), and strict JSON (a non-finite float
    raises: normalize first, never hash something that isn't what's stored)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def result_digest(value) -> str:
    """sha256 over canonical_json(value), as stored in trajectory[].result_digest."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()
