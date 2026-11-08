"""Tool retry budget and backoff (PD-13, PD-14, architecture §7 and §12.4)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tether.queue.backoff import tool_backoff_seconds
from tether.sdk import ToolContext, ToolDescriptor


class _In(BaseModel):
    x: int


async def _handler(args: _In, ctx: ToolContext) -> _In:
    return args


def _descriptor(**overrides: object) -> ToolDescriptor:
    fields: dict[str, object] = {
        "name": "probe",
        "version": "1.0.0",
        "description": "probe",
        "input_model": _In,
        "output_model": _In,
        "effect": "read",
        "handler": _handler,
    }
    fields.update(overrides)
    return ToolDescriptor(**fields)  # type: ignore[arg-type]


def test_t03_a_tool_sends_at_most_three_times_by_default() -> None:
    assert _descriptor().max_attempts == 3


def test_a_tool_may_declare_its_own_send_budget() -> None:
    assert _descriptor(max_attempts=5).max_attempts == 5


@pytest.mark.parametrize("attempts", [0, -1])
def test_a_send_budget_below_one_is_rejected(attempts: int) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        _descriptor(max_attempts=attempts)


@pytest.mark.parametrize(
    ("attempt", "ceiling"), [(1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0), (5, 5.0), (9, 5.0)]
)
def test_backoff_doubles_from_half_a_second_up_to_a_five_second_cap(
    attempt: int, ceiling: float
) -> None:
    assert tool_backoff_seconds(attempt, rng=lambda: 1.0) == pytest.approx(ceiling)


def test_backoff_uses_full_jitter_between_zero_and_the_ceiling() -> None:
    assert tool_backoff_seconds(3, rng=lambda: 0.0) == 0.0
    assert tool_backoff_seconds(3, rng=lambda: 0.25) == pytest.approx(0.5)
    for _ in range(200):
        assert 0.0 <= tool_backoff_seconds(4) <= 4.0
