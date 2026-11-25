"""Kubernetes resource quantities, read back exactly (decision 0025 D§11.6, M8B-1 amendment).

The resources postcondition compares what the API server returns against the integer the
action asked for. The server may re-canonicalize a quantity (1024Mi comes back as 1Gi), so
the comparison is on the exact value, never on the text.

The supported forms, and nothing else:

    CPU     bare decimal cores      "1", "0.5", "2.25"     -> millicores
            integer millicores      "500m"
    memory  bare decimal bytes      "1073741824", "1.5"    -> bytes
            binary suffixes         Ki Mi Gi Ti Pi Ei      (2^10, 2^20, ... 2^60)
            decimal suffixes        k  M  G  T  P  E       (10^3, 10^6, ... 10^18)

A decimal suffix is never a binary one: 1G is 10^9 bytes and 1Gi is 2^30, so they differ;
1M (10^6) differs from 1Mi (2^20). Any other form (an exponent, a sign, a CPU suffix on a
memory quantity, a memory suffix on a CPU quantity, a fractional millicore) returns None:
the postcondition can't be established from it, and nothing crashes. Milestone 8B-2 checks
the forms the real API server returns for these workloads and amends this contract if it
returns another one.

Exact arithmetic only (Fraction); no float ever enters a comparison.
"""

from __future__ import annotations

import re
from fractions import Fraction

_DECIMAL = r"[0-9]+(?:\.[0-9]+)?"
_CPU_CORES = re.compile(_DECIMAL)
_CPU_MILLIS = re.compile(r"([0-9]+)m")
_MEMORY = re.compile(rf"({_DECIMAL})(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?")

BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
DECIMAL = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18}
MIB = 2**20


def parse_cpu(value: object) -> Fraction | None:
    """Millicores of a CPU quantity, exactly; None for any unsupported form."""
    if type(value) is not str:
        return None
    if _CPU_CORES.fullmatch(value):
        return Fraction(value) * 1000
    match = _CPU_MILLIS.fullmatch(value)
    if match:
        return Fraction(int(match.group(1)))
    return None


def parse_memory(value: object) -> Fraction | None:
    """Bytes of a memory quantity, exactly; None for any unsupported form."""
    if type(value) is not str:
        return None
    match = _MEMORY.fullmatch(value)
    if match is None:
        return None
    number, suffix = match.groups()
    if suffix is None:
        return Fraction(number)
    multiplier = BINARY[suffix] if suffix in BINARY else DECIMAL[suffix]
    return Fraction(number) * multiplier


def cpu_is(value: object, millis: int) -> bool:
    """Is `value` exactly `millis` millicores?"""
    parsed = parse_cpu(value)
    return parsed is not None and parsed == millis


def memory_is(value: object, mib: int) -> bool:
    """Is `value` exactly `mib` MiB?"""
    parsed = parse_memory(value)
    return parsed is not None and parsed == mib * MIB
