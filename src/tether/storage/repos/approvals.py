"""Approval requests bound to a call hash (ADR-0010, ADR-0011, §14)."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import RowMapping

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
