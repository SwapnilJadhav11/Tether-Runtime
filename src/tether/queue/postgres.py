"""Minimal Postgres job queue: lease, complete, release (ADR-0004).

At-least-once and polling-only. Leasing spans workspaces by design (a worker serves all of them),
so these functions work on the ``jobs`` table directly; job handlers then act through repositories
scoped to the job's workspace. Every time check uses ``clock_timestamp()`` (invariant 29).

Bounded delivery (§11 AD-08): every lease adds an attempt. A lease that takes ``attempts`` past
``max_attempts`` hands the job to the worker's TX12 dead-letter transition instead of its handler;
that transaction marks the job ``dead``. A retry is a release with a delayed ``run_after``.
Not yet: lease heartbeats.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.core.statuses import JobState
from tether.storage import tables

_JOBS = tables.jobs
_RUNS = tables.runs


@dataclass(frozen=True)
class LeasedJob:
    id: uuid.UUID
    kind: str
    workspace_id: str
    run_id: uuid.UUID
    # Read from the run row itself, so the handler can check both agree (WorkspaceScope.for_job).
    run_workspace_id: str
    lease_token: uuid.UUID
    attempts: int
    max_attempts: int
    ref_id: uuid.UUID | None
    payload: dict[str, Any]


def _seconds_from_now(seconds: float) -> Any:
    return func.clock_timestamp() + func.make_interval(0, 0, 0, 0, 0, 0, seconds)


async def lease_one(
    engine: AsyncEngine, *, kinds: Sequence[str], lease_seconds: float
) -> LeasedJob | None:
    """Lease the next due job of one of ``kinds``, or a job whose lease has expired.

    An exhausted job is leased too (``attempts > max_attempts`` afterwards): the caller must
    dead-letter it (TX12), never run its handler.
    """
    now = func.clock_timestamp()
    candidate = (
        select(_JOBS.c.id)
        .where(
            _JOBS.c.kind.in_(list(kinds)),
            or_(
                and_(_JOBS.c.state == JobState.READY, _JOBS.c.run_after <= now),
                and_(_JOBS.c.state == JobState.LEASED, _JOBS.c.lease_expires_at <= now),
            ),
        )
        .order_by(_JOBS.c.run_after)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    statement = (
        update(_JOBS)
        .where(_JOBS.c.id == candidate, _RUNS.c.id == _JOBS.c.run_id)
        .values(
            state=JobState.LEASED,
            lease_token=uuid.uuid4(),
            lease_expires_at=_seconds_from_now(lease_seconds),
            attempts=_JOBS.c.attempts + 1,
        )
        .returning(
            _JOBS.c.id,
            _JOBS.c.kind,
            _JOBS.c.workspace_id,
            _JOBS.c.run_id,
            _RUNS.c.workspace_id.label("run_workspace_id"),
            _JOBS.c.lease_token,
            _JOBS.c.attempts,
            _JOBS.c.max_attempts,
            _JOBS.c.ref_id,
            _JOBS.c.payload,
        )
    )
    async with engine.begin() as connection:
        row = (await connection.execute(statement)).mappings().first()
    return None if row is None else LeasedJob(**row)


async def complete(engine: AsyncEngine, job: LeasedJob) -> bool:
    """Mark the job done; refused if this lease is no longer the current one."""
    statement = (
        update(_JOBS)
        .where(
            _JOBS.c.id == job.id,
            _JOBS.c.state == JobState.LEASED,
            _JOBS.c.lease_token == job.lease_token,
        )
        .values(state=JobState.DONE, lease_token=None, lease_expires_at=None)
        .returning(_JOBS.c.id)
    )
    async with engine.begin() as connection:
        return (await connection.execute(statement)).first() is not None


async def release(
    engine: AsyncEngine, job: LeasedJob, *, delay_seconds: float, refund_attempt: bool
) -> bool:
    """Return the job to ``ready`` after ``delay_seconds``; refused if the lease is stale."""
    attempts = _JOBS.c.attempts - 1 if refund_attempt else _JOBS.c.attempts
    statement = (
        update(_JOBS)
        .where(
            _JOBS.c.id == job.id,
            _JOBS.c.state == JobState.LEASED,
            _JOBS.c.lease_token == job.lease_token,
        )
        .values(
            state=JobState.READY,
            run_after=_seconds_from_now(delay_seconds),
            lease_token=None,
            lease_expires_at=None,
            attempts=attempts,
        )
        .returning(_JOBS.c.id)
    )
    async with engine.begin() as connection:
        return (await connection.execute(statement)).first() is not None
