"""Minimal run driver for ``advance_run`` jobs (ADR-0005, ADR-0006).

Lease, then lock: after the job is leased, the driver takes a session-level advisory lock on the
run, on a dedicated connection held for the whole invocation. With the lock held it takes the next
fencing token (``driver_generation``), marks the run ``running`` and drives the run graph
(``tether.runtime.driver``). Every write the graph makes asserts that token.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tether.policy.engine import PolicyStaleError
from tether.queue.postgres import LeasedJob
from tether.runtime.driver import WorkerRuntime, drive
from tether.storage.repos.runs import FencedError, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

_TRY_LOCK = text("SELECT pg_try_advisory_lock(hashtextextended(:name, 0))")
_UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(:name, 0))")


class Outcome(StrEnum):
    COMPLETED = "completed"  # the job's work is done (or there was nothing left to do)
    CONTENDED = "contended"  # another driver holds the run lock
    FENCED = "fenced"  # a newer driver took over during this invocation
    STALE = "stale"  # this process's policy is not the active version: decide nothing (ADR-0009)


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


async def handle(runtime: WorkerRuntime, job: LeasedJob) -> Outcome:
    engine = runtime.engine
    scope = WorkspaceScope.for_job(
        job_workspace_id=job.workspace_id, run_workspace_id=job.run_workspace_id
    )
    async with run_lock(engine, job.run_id) as acquired:
        if not acquired:
            return Outcome.CONTENDED
        generation = await start(engine, scope, job.run_id)
        if generation is None:
            return Outcome.COMPLETED
        try:
            await drive(runtime, scope, job.run_id, generation)
        except FencedError:
            return Outcome.FENCED
        except PolicyStaleError:
            return Outcome.STALE
        return Outcome.COMPLETED
