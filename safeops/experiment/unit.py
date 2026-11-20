"""The experimental unit (design §10.1) and the bounds on its fields (decision 0014).

Pure: no I/O, no third-party imports. The CLI, the runner and the fault-spec loader all
validate through here, so a value that one of them accepts can't be refused by another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Fault seeds fit an unsigned 32-bit integer, far below JSON's exact-integer limit (2^53),
# so a seed is never rounded in a run record and any seeded RNG accepts it unchanged.
MAX_FAULT_SEED = 2**32 - 1
# trial_index is 0..k-1, and k is set from pilot variance (§18). It drives no RNG.
MAX_TRIAL_INDEX = 999

# A fault id names a file under faults/, so it must not be able to name anything else.
FAULT_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)


class InvalidRunUnit(ValueError):
    """A field of the experimental unit is outside its declared range."""


def validate_fault_id(fault_id: object) -> str:
    """Return ``fault_id`` if it is a strict fault id, else raise InvalidRunUnit.

    Lowercase ASCII letters, digits and inner hyphens, 1 to 64 characters. No dots,
    slashes, whitespace or trailing newline, so it can never escape faults/.
    """
    if not isinstance(fault_id, str) or not FAULT_ID_PATTERN.fullmatch(fault_id):
        raise InvalidRunUnit(
            f"invalid fault id {fault_id!r}: use 1-64 lowercase letters, digits and inner"
            " hyphens (e.g. bad-deployment-config)"
        )
    return fault_id


def _bounded(name: str, value: object, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= hi:
        raise InvalidRunUnit(f"{name} must be an integer in 0..{hi}, got {value!r}")
    return value


@dataclass(frozen=True)
class RunUnit:
    """The experimental unit (§10.1): which scenario, which fault construction, which
    repetition over agent nondeterminism."""

    fault_id: str
    fault_seed: int
    trial_index: int

    def __post_init__(self) -> None:
        validate_fault_id(self.fault_id)
        _bounded("fault_seed", self.fault_seed, MAX_FAULT_SEED)
        _bounded("trial_index", self.trial_index, MAX_TRIAL_INDEX)
