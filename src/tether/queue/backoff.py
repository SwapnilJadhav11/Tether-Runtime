"""Retry delays (PD-13). A retry is a delayed job: the delay becomes the job's ``run_after``."""

from __future__ import annotations

import random
from collections.abc import Callable

from tether.sdk import VerifyBackoff

# PD-13, tools: base 0.5 s, doubling per attempt, capped at 5 s, full jitter.
TOOL_BASE_SECONDS = 0.5
TOOL_CAP_SECONDS = 5.0

_RANDOM = random.SystemRandom()


def verify_backoff_seconds(
    declared: VerifyBackoff, attempt: int, *, rng: Callable[[], float] | None = None
) -> float:
    """Delay before the probe after probe ``attempt`` (1-based) was inconclusive: the declaration's
    base * multiplier^(attempt-1), capped, full jitter (ADR-0019 rule 9)."""
    exponent = max(attempt - 1, 0)
    ceiling = min(declared.cap_seconds, declared.base_seconds * declared.multiplier**exponent)
    draw: float = rng() if rng is not None else _RANDOM.random()
    return draw * ceiling


def tool_backoff_seconds(attempt: int, *, rng: Callable[[], float] | None = None) -> float:
    """Delay before re-sending after send ``attempt`` (1-based) failed: uniform in [0, ceiling]."""
    ceiling = min(TOOL_CAP_SECONDS, TOOL_BASE_SECONDS * 2.0 ** max(attempt - 1, 0))
    draw: float = rng() if rng is not None else _RANDOM.random()
    return draw * ceiling
