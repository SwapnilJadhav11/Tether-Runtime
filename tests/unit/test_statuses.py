"""S1.5: status value sets from PD-18, shared by the schema CHECK constraints and the runtime."""

from __future__ import annotations

from enum import StrEnum

from tether.core.statuses import (
    TERMINAL_RUN_STATUSES,
    ApprovalStatus,
    JobKind,
    JobState,
    LedgerResolution,
    LedgerState,
    RunStatus,
)


def values(enum: type[StrEnum]) -> set[str]:
    return {member.value for member in enum}


def test_run_statuses() -> None:
    assert values(RunStatus) == {
        "queued",
        "running",
        "awaiting_approval",
        "awaiting_tool",
        "awaiting_reconciliation",
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
        "abandoned",
    }


def test_terminal_run_statuses() -> None:
    assert set(TERMINAL_RUN_STATUSES) == {
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
        "abandoned",
    }
    assert values(RunStatus) >= TERMINAL_RUN_STATUSES


def test_ledger_states_and_resolutions() -> None:
    assert values(LedgerState) == {
        "pending",
        "dispatched",
        "completed",
        "failed",
        "indeterminate",
        "abandoned",
    }
    assert values(LedgerResolution) == {
        "automatic",
        "reconciled_applied",
        "reconciled_not_applied",
        "reconciled_abandon",
    }


def test_approval_statuses() -> None:
    assert values(ApprovalStatus) == {"pending", "approved", "rejected", "expired", "closed"}


def test_job_states_and_kinds() -> None:
    assert values(JobState) == {"ready", "leased", "done", "dead"}
    assert values(JobKind) == {"advance_run", "execute_tool", "expire_approval", "run_deadline"}
