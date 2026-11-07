"""Status value sets (PD-18). The schema's CHECK constraints accept exactly these values."""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_TOOL = "awaiting_tool"
    AWAITING_RECONCILIATION = "awaiting_reconciliation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"


# Once a run reaches one of these, its status never changes (invariant 8, ADR-0006).
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
        RunStatus.ABANDONED,
    }
)


class LedgerState(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"
    ABANDONED = "abandoned"


class LedgerResolution(StrEnum):
    AUTOMATIC = "automatic"
    RECONCILED_APPLIED = "reconciled_applied"
    RECONCILED_NOT_APPLIED = "reconciled_not_applied"
    RECONCILED_ABANDON = "reconciled_abandon"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CLOSED = "closed"


class JobState(StrEnum):
    READY = "ready"
    LEASED = "leased"
    DONE = "done"
    DEAD = "dead"


class JobKind(StrEnum):
    ADVANCE_RUN = "advance_run"
    EXECUTE_TOOL = "execute_tool"
    EXPIRE_APPROVAL = "expire_approval"
    RUN_DEADLINE = "run_deadline"
