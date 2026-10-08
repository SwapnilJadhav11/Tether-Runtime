"""S1.4: the neutral tool-result outcome enum (AD-06, PD-18)."""

from __future__ import annotations

from tether.core.results import CallOutcome


def test_call_outcomes_match_the_architecture() -> None:
    assert {outcome.value for outcome in CallOutcome} == {
        "applied",
        "not_applied",
        "failed",
        "denied",
        "invalid",
        "rejected",
        "expired",
        "reconciled",
        "skipped",
    }
