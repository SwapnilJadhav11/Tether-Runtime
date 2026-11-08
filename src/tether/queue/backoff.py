"""Retry delays (PD-13). A retry is a delayed job: the delay becomes the job's ``run_after``."""

from __future__ import annotations

import random
from collections.abc import Callable

# PD-13, tools: base 0.5 s, doubling per attempt, capped at 5 s, full jitter.
TOOL_BASE_SECONDS = 0.5
TOOL_CAP_SECONDS = 5.0

_RANDOM = random.SystemRandom()


def tool_backoff_seconds(attempt: int, *, rng: Callable[[], float] | None = None) -> float:
    """Delay before re-sending after send ``attempt`` (1-based) failed: uniform in [0, ceiling]."""
    ceiling = min(TOOL_CAP_SECONDS, TOOL_BASE_SECONDS * 2.0 ** max(attempt - 1, 0))
    draw: float = rng() if rng is not None else _RANDOM.random()
    return draw * ceiling
