"""Minimal run driver for ``advance_run`` jobs (ADR-0005, ADR-0006).

Lease, then lock: after the job is leased, the driver takes a session-level advisory lock on the
run, on a dedicated connection held for the whole invocation. With the lock held it takes the next
fencing token (``driver_generation``) and moves ``queued``/``running`` to ``running``, then
``running`` to ``succeeded`` only while that token is still current.

Walking-skeleton scope: no graph, model calls, policy, approvals, tools or audit events yet.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tether.core.statuses import RunStatus
from tether.queue.postgres import LeasedJob
from tether.storage.repos.runs import RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

_TRY_LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:name, 0))")
_UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(:name, 0))")


class Outcome(StrEnum):
    COMPLETED = "completed"  # the job's work is done (or there was nothing left to do)
    CONTENDED = "contended"  # another driver holds the run lock
    FENCED = "fenced"  # a newer driver took over during this invocation


def run_lock_name(run_id: uuid.UUID) -> str:
    return f"tether.run:{run_id}"


async def _unlock(connection: AsyncConnection, name: str) -> None:
    # A connection must never go back to the pool still holding the session lock.
    try:
        released = (await connection.execute(_UNLOCK, {"name": name})).scalar()
    except BaseException:
        await connection.invalidate()
        raise
    if not released:
        await connection.invalidate()


@asynccontextmanager
async def run_lock(engine: AsyncEngine, run_id: uuid.UUID) -> AsyncIterator[bool]:
    """Try the per-run advisory lock; yields whether it was acquired."""
    name = run_lock_name(run_id)
    async with engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        acquired = bool((await connection.execute(_TRY_LOCK, {"name": name})).scalar())
        try:
            yield acquired
        finally:
            if acquired:
                await _unlock(connection, name)


async def start(engine: AsyncEngine, scope: WorkspaceScope, run_id: uuid.UUID) -> int | None:
    """Take the next fencing token; ``None`` if the run is terminal or not in scope."""
    async with UnitOfWork(engine) as uow:
        return await uow.repository(RunsRepository, scope).claim_driver_generation(run_id)


async def finish(
    engine: AsyncEngine, scope: WorkspaceScope, run_id: uuid.UUID, generation: int
) -> bool:
    """``running`` to ``succeeded``, refused if ``generation`` has been superseded."""
    async with UnitOfWork(engine) as uow:
        return await uow.repository(RunsRepository, scope).transition(
            run_id, expected=RunStatus.RUNNING, new=RunStatus.SUCCEEDED, generation=generation
        )


async def handle(engine: AsyncEngine, job: LeasedJob) -> Outcome:
    scope = WorkspaceScope.for_job(
        job_workspace_id=job.workspace_id, run_workspace_id=job.run_workspace_id
    )
    async with run_lock(engine, job.run_id) as acquired:
        if not acquired:
            return Outcome.CONTENDED
        generation = await start(engine, scope, job.run_id)
        if generation is None:
            return Outcome.COMPLETED
        if not await finish(engine, scope, job.run_id, generation):
            return Outcome.FENCED
        return Outcome.COMPLETED
