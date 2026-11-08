"""Execution ledger entries (ADR-0007, §12)."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import RowMapping

from tether.core.statuses import LedgerState
from tether.storage import tables
from tether.storage.repos.base import ScopedRepository

# A run cannot finish while one of its entries is in one of these states.
UNRESOLVED_STATES = (LedgerState.PENDING, LedgerState.DISPATCHED, LedgerState.INDETERMINATE)


class LedgerRepository(ScopedRepository):
    table = tables.ledger_entries

    async def for_run(self, run_id: uuid.UUID) -> list[RowMapping]:
        statement = self.scoped(
            select(self.table)
            .where(self.table.c.run_id == run_id)
            .order_by(self.table.c.step_ordinal)
        )
        return list((await self._connection.execute(statement)).mappings().all())

    async def by_ordinal(self, run_id: uuid.UUID, step_ordinal: int) -> RowMapping | None:
        statement = self.scoped(
            select(self.table).where(
                self.table.c.run_id == run_id, self.table.c.step_ordinal == step_ordinal
            )
        )
        row = (await self._connection.execute(statement)).mappings().first()
        return cast("RowMapping | None", row)

    async def unresolved(self, run_id: uuid.UUID) -> bool:
        """Whether the run has an entry still in flight or ``indeterminate`` (ADR-0004 TX12)."""
        statement = self.scoped(
            select(self.table.c.id)
            .where(
                self.table.c.run_id == run_id,
                self.table.c.state.in_(UNRESOLVED_STATES),
            )
            .limit(1)
        )
        return (await self._connection.execute(statement)).first() is not None

    async def lock(self, entry_id: uuid.UUID) -> RowMapping | None:
        statement = self.scoped(select(self.table).where(self.table.c.id == entry_id))
        result = await self._connection.execute(statement.with_for_update())
        return cast("RowMapping | None", result.mappings().first())

    async def transition(
        self, entry_id: uuid.UUID, *, expected: str, new: str, **values: Any
    ) -> bool:
        """Conditional state change: refused unless the entry is still in ``expected``."""
        ledger = self.table
        statement = (
            update(ledger)
            .where(
                ledger.c.workspace_id == self.scope.workspace_id,
                ledger.c.id == entry_id,
                ledger.c.state == expected,
            )
            .values(state=new, **values)
            .returning(ledger.c.id)
        )
        return (await self._connection.execute(statement)).first() is not None
