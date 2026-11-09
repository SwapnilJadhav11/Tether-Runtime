"""Workspace-scoped repositories for runs and the rows that hang off them."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import Select, and_, case, func, or_, select, update
from sqlalchemy.engine import RowMapping

from tether.core.statuses import TERMINAL_RUN_STATUSES, JobKind, JobState, RunStatus
from tether.observability import tracing
from tether.storage import tables
from tether.storage.repos.base import ScopedRepository

NON_TERMINAL = [status for status in RunStatus if status not in TERMINAL_RUN_STATUSES]


class FencedError(Exception):
    """A newer run driver holds this run: this invocation must stop writing (ADR-0005)."""


class RunsRepository(ScopedRepository):
    table = tables.runs

    async def claim_driver_generation(self, run_id: uuid.UUID) -> int | None:
        """Mark a non-terminal run ``running`` and take the next fencing token (ADR-0005).

        Returns ``None`` if the run is not found in scope or is terminal.
        """
        runs = self.table
        statement = (
            update(runs)
            .where(
                runs.c.workspace_id == self.scope.workspace_id,
                runs.c.id == run_id,
                runs.c.status.in_(NON_TERMINAL),
            )
            .values(status=RunStatus.RUNNING, driver_generation=runs.c.driver_generation + 1)
            .returning(runs.c.driver_generation)
        )
        result = await self._connection.execute(statement)
        generation = result.scalar_one_or_none()
        return None if generation is None else int(generation)

    async def fence(self, run_id: uuid.UUID) -> tuple[int, RunStatus] | None:
        """Take the next fencing token without changing the status (TX12, ADR-0005).

        Returns the new token and the current status, or ``None`` if the run is terminal.
        """
        runs = self.table
        statement = (
            update(runs)
            .where(
                runs.c.workspace_id == self.scope.workspace_id,
                runs.c.id == run_id,
                runs.c.status.in_(NON_TERMINAL),
            )
            .values(driver_generation=runs.c.driver_generation + 1)
            .returning(runs.c.driver_generation, runs.c.status)
        )
        row = (await self._connection.execute(statement)).first()
        return None if row is None else (int(row[0]), RunStatus(row[1]))

    def _with_stop(self) -> Select[Any]:
        """The run row plus the stop predicate by database time (ADR-0006, invariant 29).

        ``stopped``: cancel requested or the deadline reached. ``stop_status``: the terminal status
        a stop leads to; whichever stop came first wins.
        """
        runs = self.table
        deadline_reached = func.clock_timestamp() >= runs.c.deadline_at
        cancelled_first = and_(
            runs.c.cancel_requested_at.is_not(None),
            runs.c.cancel_requested_at <= runs.c.deadline_at,
        )
        return select(
            runs,
            or_(runs.c.cancel_requested_at.is_not(None), deadline_reached).label("stopped"),
            deadline_reached.label("deadline_reached"),
            case(
                (cancelled_first, RunStatus.CANCELLED.value),
                (deadline_reached, RunStatus.TIMED_OUT.value),
                (runs.c.cancel_requested_at.is_not(None), RunStatus.CANCELLED.value),
            ).label("stop_status"),
        )

    async def share_lock(self, run_id: uuid.UUID) -> RowMapping:
        """Share-lock the run row (``FOR SHARE``). ``stopped`` is the stop predicate now."""
        statement = self.scoped(self._with_stop().where(self.table.c.id == run_id))
        statement = statement.with_for_update(read=True)
        return cast("RowMapping", (await self._connection.execute(statement)).mappings().one())

    async def lock(self, run_id: uuid.UUID) -> RowMapping | None:
        """Lock the run row ``FOR UPDATE`` (TX9, TX10, TX-EA); ``None`` if not in scope."""
        statement = self.scoped(self._with_stop().where(self.table.c.id == run_id))
        row = (await self._connection.execute(statement.with_for_update())).mappings().first()
        return cast("RowMapping | None", row)

    async def request_cancel(self, run_id: uuid.UUID, requested_by: str) -> None:
        """TX9's stop flag. A request, never a status: only the run driver writes status."""
        runs = self.table
        await self._connection.execute(
            update(runs)
            .where(
                runs.c.workspace_id == self.scope.workspace_id,
                runs.c.id == run_id,
                runs.c.cancel_requested_at.is_(None),
            )
            .values(cancel_requested_at=func.clock_timestamp(), cancel_requested_by=requested_by)
        )

    async def record_deadline_exceeded(self, run_id: uuid.UUID) -> None:
        """TX10's marker: the stored deadline has passed (the deadline itself never changes)."""
        runs = self.table
        await self._connection.execute(
            update(runs)
            .where(
                runs.c.workspace_id == self.scope.workspace_id,
                runs.c.id == run_id,
                runs.c.deadline_exceeded_at.is_(None),
            )
            .values(deadline_exceeded_at=func.clock_timestamp())
        )

    async def guard(self, run_id: uuid.UUID, generation: int) -> RowMapping:
        """``share_lock`` for a run-driver write, asserting the fencing token (ADR-0005)."""
        row = await self.share_lock(run_id)
        if row["driver_generation"] != generation:
            raise FencedError(f"run {run_id} is driven by generation {row['driver_generation']}")
        return row

    async def transition(
        self,
        run_id: uuid.UUID,
        *,
        expected: RunStatus | Iterable[RunStatus],
        new: RunStatus,
        generation: int,
        **values: Any,
    ) -> bool:
        """Conditional status change, refused unless ``generation`` is still current."""
        runs = self.table
        allowed = [expected] if isinstance(expected, RunStatus) else list(expected)
        statement = (
            update(runs)
            .where(
                runs.c.workspace_id == self.scope.workspace_id,
                runs.c.id == run_id,
                runs.c.status.in_(allowed),
                runs.c.driver_generation == generation,
            )
            .values(status=new, **values)
            .returning(runs.c.id)
        )
        result = await self._connection.execute(statement)
        return result.scalar_one_or_none() is not None


# PD-14: delivery attempts per job kind.
MAX_ATTEMPTS = {
    JobKind.ADVANCE_RUN: 8,
    JobKind.EXECUTE_TOOL: 5,
    JobKind.EXPIRE_APPROVAL: 10,
    JobKind.RUN_DEADLINE: 10,
}


class JobsRepository(ScopedRepository):
    table = tables.jobs

    async def enqueue(
        self,
        kind: JobKind,
        run_id: uuid.UUID,
        *,
        ref_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        run_after: Any = None,
    ) -> None:
        """Insert a job in the caller's transaction; ``run_after`` delays it (a timer)."""
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "kind": kind,
            "run_id": run_id,
            "ref_id": ref_id,
            "payload": payload or {},
            "max_attempts": MAX_ATTEMPTS[kind],
            # The enqueuing span: the job's span links to it (AD-13); telemetry only.
            "trace_parent": tracing.current_traceparent(),
        }
        if run_after is not None:
            values["run_after"] = run_after
        await self.add(values)

    async def mark_dead(self, job_id: uuid.UUID, lease_token: uuid.UUID) -> bool:
        """TX12's last write: ``leased → dead``, refused unless this lease is still current."""
        jobs = self.table
        statement = (
            update(jobs)
            .where(
                jobs.c.workspace_id == self.scope.workspace_id,
                jobs.c.id == job_id,
                jobs.c.state == JobState.LEASED,
                jobs.c.lease_token == lease_token,
            )
            .values(state=JobState.DEAD, lease_token=None, lease_expires_at=None)
            .returning(jobs.c.id)
        )
        return (await self._connection.execute(statement)).first() is not None


class ModelCallsRepository(ScopedRepository):
    table = tables.model_calls


class PolicyVersionsRepository(ScopedRepository):
    table = tables.policy_versions
    key_column = "version"

    async def active_version(self) -> str | None:
        """The active version, share-locked for the decision transaction (PD-31)."""
        statement = self.scoped(
            select(self.table.c.version).where(self.table.c.active)
        ).with_for_update(read=True)
        version = (await self._connection.execute(statement)).scalar_one_or_none()
        return None if version is None else str(version)
