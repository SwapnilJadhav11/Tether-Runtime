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

from tether.core.statuses import JobKind, RunStatus
from tether.policy.engine import PolicyStaleError
from tether.queue.postgres import LeasedJob
from tether.runtime.driver import WorkerRuntime, drive
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import FencedError, JobsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

# ADR-0004: the run outcome when ``advance_run`` exhausts its deliveries with nothing in flight.
SYSTEM_ERROR = "system_error"

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
    """Take the next fencing token; ``None`` if the run is terminal, held, or not in scope.

    A run ``awaiting_reconciliation`` stays held while any of its ledger entries is in flight or
    ``indeterminate``; only that entry's own outcome or a reconciliation, each with its own wake,
    lets a later wake resume it (ADR-0004 TX12, plan PD-35).
    """
    async with UnitOfWork(engine) as uow:
        runs = uow.repository(RunsRepository, scope)
        run = await runs.get(run_id)
        if (
            run is not None
            and run["status"] == RunStatus.AWAITING_RECONCILIATION
            and await uow.repository(LedgerRepository, scope).unresolved(run_id)
        ):
            return None
        return await runs.claim_driver_generation(run_id)


class _StaleLease(Exception):  # noqa: N818 - a control-flow signal that rolls TX12 back
    """Another lessee owns the job now: TX12 must not commit."""


async def dead_letter(runtime: WorkerRuntime, job: LeasedJob) -> Outcome:
    """TX12 for ``advance_run`` (ADR-0004): the run driver's dead-letter function.

    Under the run's advisory lock and a fresh fencing token, in the transaction that marks the job
    ``dead``: the run moves to ``awaiting_reconciliation`` if a ledger entry is in flight or
    ``indeterminate``, otherwise to ``failed`` with reason ``system_error``; audited, with a wake.
    Refused as a whole if this lease is no longer current.
    """
    engine = runtime.engine
    scope = WorkspaceScope.for_job(
        job_workspace_id=job.workspace_id, run_workspace_id=job.run_workspace_id
    )
    async with run_lock(engine, job.run_id) as acquired:
        if not acquired:
            return Outcome.CONTENDED
        try:
            async with UnitOfWork(engine) as uow:
                await _dead_letter_run(uow, runtime, scope, job)
                if not await uow.repository(JobsRepository, scope).mark_dead(
                    job.id, job.lease_token
                ):
                    raise _StaleLease
        except _StaleLease:
            pass
    return Outcome.COMPLETED


async def _dead_letter_run(
    uow: UnitOfWork, runtime: WorkerRuntime, scope: WorkspaceScope, job: LeasedJob
) -> None:
    runs = uow.repository(RunsRepository, scope)
    fenced = await runs.fence(job.run_id)
    if fenced is None:
        return  # already terminal: its outcome is recorded
    generation, status = fenced
    held = await uow.repository(LedgerRepository, scope).unresolved(job.run_id)
    new = RunStatus.AWAITING_RECONCILIATION if held else RunStatus.FAILED
    summary = None if held else SYSTEM_ERROR
    await runs.transition(
        job.run_id, expected=status, new=new, generation=generation, outcome_summary=summary
    )
    audit = uow.repository(AuditRepository, scope)
    await audit.append(
        runtime.keys,
        run_id=job.run_id,
        dedupe_key=f"dead_letter:{job.id}",
        event_type="DEAD_LETTERED",
        payload={
            "job_id": str(job.id),
            "kind": job.kind,
            "attempts": job.attempts,
            "status": new.value,
        },
    )
    if not held:
        await audit.append(
            runtime.keys,
            run_id=job.run_id,
            dedupe_key="run_finished",
            event_type="RUN_FINISHED",
            payload={"status": new.value, "summary": SYSTEM_ERROR},
        )
    await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, job.run_id)


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
