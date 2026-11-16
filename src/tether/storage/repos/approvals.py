"""Approval requests bound to a call hash (ADR-0010, ADR-0011, §14)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Select, and_, func, select, tuple_, update
from sqlalchemy.engine import RowMapping

from tether.core.statuses import TERMINAL_RUN_STATUSES
from tether.storage import tables
from tether.storage.repos.base import ScopedRepository


class ApprovalsRepository(ScopedRepository):
    table = tables.approvals

    async def for_run(self, run_id: uuid.UUID) -> list[RowMapping]:
        statement = self.scoped(
            select(self.table)
            .where(self.table.c.run_id == run_id)
            .order_by(self.table.c.step_ordinal)
        )
        return list((await self._connection.execute(statement)).mappings().all())

    def _with_liveness(self) -> list[Any]:
        # ``live``: not yet expired, by database time (invariant 29).
        return [self.table, (func.clock_timestamp() < self.table.c.expires_at).label("live")]

    def _with_run(self) -> Select[Any]:
        """Approval columns plus the run's requester, agent and stop state (read-only joins).

        ``run_active``: the run is neither terminal nor stopped (cancel requested or deadline
        reached), by database time (ADR-0006).
        """
        approvals, runs = self.table, tables.runs
        run_active = and_(
            runs.c.cancel_requested_at.is_(None),
            func.clock_timestamp() < runs.c.deadline_at,
            runs.c.status.not_in([str(status) for status in TERMINAL_RUN_STATUSES]),
        )
        return select(
            approvals,
            (func.clock_timestamp() < approvals.c.expires_at).label("live"),
            run_active.label("run_active"),
            runs.c.requester_snapshot,
            runs.c.agent_id,
        ).join(
            runs,
            and_(runs.c.workspace_id == approvals.c.workspace_id, runs.c.id == approvals.c.run_id),
        )

    async def detail(self, approval_id: uuid.UUID) -> RowMapping | None:
        statement = self.scoped(self._with_run().where(self.table.c.id == approval_id))
        return cast(
            "RowMapping | None", (await self._connection.execute(statement)).mappings().first()
        )

    async def pending_after(
        self, after: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> list[RowMapping]:
        """Pending, unexpired approvals of runs still active, oldest first, after a cursor."""
        approvals = self.table
        statement = self._with_run().where(
            approvals.c.status == "pending",
            func.clock_timestamp() < approvals.c.expires_at,
            tables.runs.c.cancel_requested_at.is_(None),
            func.clock_timestamp() < tables.runs.c.deadline_at,
            tables.runs.c.status.not_in([str(status) for status in TERMINAL_RUN_STATUSES]),
        )
        if after is not None:
            statement = statement.where(
                tuple_(approvals.c.created_at, approvals.c.id) > tuple_(*after)
            )
        statement = self.scoped(
            statement.order_by(approvals.c.created_at, approvals.c.id).limit(limit)
        )
        return list((await self._connection.execute(statement)).mappings().all())

    async def by_ordinal(self, run_id: uuid.UUID, step_ordinal: int) -> RowMapping | None:
        statement = self.scoped(
            select(*self._with_liveness()).where(
                self.table.c.run_id == run_id, self.table.c.step_ordinal == step_ordinal
            )
        )
        row = (await self._connection.execute(statement)).mappings().first()
        return cast("RowMapping | None", row)

    async def lock(self, approval_id: uuid.UUID) -> RowMapping | None:
        statement = self.scoped(
            select(*self._with_liveness()).where(self.table.c.id == approval_id)
        ).with_for_update()
        row = (await self._connection.execute(statement)).mappings().first()
        return cast("RowMapping | None", row)

    async def close_pending(self, run_id: uuid.UUID) -> list[RowMapping]:
        """TX9/TX10: every pending approval of the run becomes ``closed`` (non-actionable)."""
        approvals = self.table
        statement = (
            update(approvals)
            .where(
                approvals.c.workspace_id == self.scope.workspace_id,
                approvals.c.run_id == run_id,
                approvals.c.status == "pending",
            )
            .values(status="closed")
            .returning(approvals.c.id, approvals.c.step_ordinal, approvals.c.tool)
        )
        rows = (await self._connection.execute(statement)).mappings().all()
        return sorted(rows, key=lambda row: row["step_ordinal"])

    async def expire(self, approval_id: uuid.UUID) -> bool:
        """TX-EA: ``pending → expired`` once its expiry has passed. False if refused."""
        approvals = self.table
        statement = (
            update(approvals)
            .where(
                approvals.c.workspace_id == self.scope.workspace_id,
                approvals.c.id == approval_id,
                approvals.c.status == "pending",
                func.clock_timestamp() >= approvals.c.expires_at,
            )
            .values(status="expired")
            .returning(approvals.c.id)
        )
        return (await self._connection.execute(statement)).first() is not None

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        status: str,
        decided_by: str,
        reason: str,
        policy_version: str,
    ) -> bool:
        """``pending → approved/rejected``, only while unexpired. False if refused."""
        approvals = self.table
        statement = (
            update(approvals)
            .where(
                approvals.c.workspace_id == self.scope.workspace_id,
                approvals.c.id == approval_id,
                approvals.c.status == "pending",
                func.clock_timestamp() < approvals.c.expires_at,
            )
            .values(
                status=status,
                decided_by=decided_by,
                decided_at=func.clock_timestamp(),
                decision_reason=reason,
                policy_version=policy_version,
            )
            .returning(approvals.c.id)
        )
        return (await self._connection.execute(statement)).first() is not None
