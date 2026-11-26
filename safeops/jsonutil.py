"""Hardened JSON-object parsing, one implementation for every untrusted JSON boundary
(decision 0025 D§3; M8B-2 STOP-resolution amendment).

A neutral leaf: standard library only, no SafeOps import. The proposal parser
(safeops.agent.parser) and the executor's direct Kubernetes PATCH response classifier both use
it, so neither subsystem imports the other.

`strict_json_object(text)` returns the whole (trimmed) text as exactly one JSON object, or
raises MalformedJSON (a ValueError). Refused while parsing, before any information is lost:
- a duplicate key, at any nesting level;
- the non-finite tokens NaN, Infinity and -Infinity;
- a number that decodes to infinity (1e400);
- a huge integer literal (Python's integer-digit limit, a ValueError);
- absurd nesting (RecursionError);
- a top-level value that is not an object (a list, scalar or null).
"""

from __future__ import annotations

import json
import math


class MalformedJSON(ValueError):
    """Untrusted content the strict JSON boundary refuses."""


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise MalformedJSON("duplicate key")
    return dict(pairs)


def _refuse_constant(token: str) -> float:
    raise MalformedJSON(f"non-finite token {token}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise MalformedJSON(f"number {text} is not finite")
    return value


def strict_json_object(text: str) -> dict:
    """The whole (trimmed) text as exactly one JSON object, or MalformedJSON."""
    try:
        value = json.loads(
            text.strip(),
            object_pairs_hook=_no_duplicates,
            parse_constant=_refuse_constant,
            parse_float=_finite_float,
        )
    except MalformedJSON:
        raise
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError is a ValueError; so is Python's int-digit limit on a huge integer
        # literal. Both, and absurd nesting, are untrusted content, never a harness failure.
        raise MalformedJSON(f"not one JSON value: {exc}") from None
    if type(value) is not dict:
        raise MalformedJSON("the answer is not a JSON object")
    return value
