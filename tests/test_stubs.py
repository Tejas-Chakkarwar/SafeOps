from __future__ import annotations

import pytest

from safeops.stubs import NotImplementedStage, stub, stub_milestone


class Example:
    @stub(milestone=7)
    def check(self, x):
        """Docstring is kept."""

    def implemented(self):
        return 42


def test_stub_refuses_loudly_never_returns() -> None:
    with pytest.raises(NotImplementedStage) as exc:
        Example().check(1)
    assert exc.value.stage == "Example.check"
    assert exc.value.milestone == 7
    assert str(exc.value) == "Example.check is not implemented yet (Milestone 7)"


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [((), {}), ((1, 2), {}), ((), {"y": 1})],
    ids=["too-few", "too-many", "unknown-keyword"],
)
def test_stub_rejects_wrong_call_signature(args: tuple, kwargs: dict) -> None:
    """A wiring bug must surface as TypeError now, not hide behind 'not implemented'."""
    with pytest.raises(TypeError):
        Example().check(*args, **kwargs)


def test_stub_marker_distinguishes_stub_from_implemented() -> None:
    assert stub_milestone(Example.check) == 7
    assert stub_milestone(Example.implemented) is None
    assert Example.check.__doc__ == "Docstring is kept."


@pytest.mark.parametrize("milestone", [0, 1, 18])
def test_stub_rejects_impossible_milestones(milestone: int) -> None:
    with pytest.raises(ValueError):
        stub(milestone)
