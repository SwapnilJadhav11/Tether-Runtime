"""Base class for workspace-scoped repositories (AD-04, ADR-0013, invariant 13).

A repository cannot be constructed without a ``WorkspaceScope``. Every read is filtered by the
scope's workspace, so a row in another workspace is simply not found (the API maps that to
404). Every write is stamped with the scope's workspace and refuses a different one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, cast

from sqlalchemy import Select, Table, insert, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from tether.core.errors import ScopeViolationError
from tether.storage.scope import WorkspaceScope


class ScopedRepository:
    table: ClassVar[Table]
    key_column: ClassVar[str] = "id"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        table = cls.__dict__.get("table")
        if table is not None and "workspace_id" not in table.c:
            raise TypeError(f"{cls.__name__}: scoped repositories need a workspace_id column")

    def __init__(self, connection: AsyncConnection, scope: WorkspaceScope) -> None:
        if not isinstance(scope, WorkspaceScope):
            raise TypeError(f"{type(self).__name__} requires a WorkspaceScope")
        self._connection = connection
        self._scope = scope

    @property
    def scope(self) -> WorkspaceScope:
        return self._scope

    def scoped(self, statement: Select[Any]) -> Select[Any]:
        """Restrict a SELECT on this repository's table to the scope's workspace."""
        return statement.where(self.table.c.workspace_id == self._scope.workspace_id)

    async def get(self, key: Any) -> RowMapping | None:
        statement = self.scoped(select(self.table).where(self.table.c[self.key_column] == key))
        result = await self._connection.execute(statement)
        return cast("RowMapping | None", result.mappings().first())

    async def add(self, values: Mapping[str, Any]) -> None:
        workspace_id = values.get("workspace_id", self._scope.workspace_id)
        if workspace_id != self._scope.workspace_id:
            raise ScopeViolationError("refusing to write a row for another workspace")
        row = {**values, "workspace_id": self._scope.workspace_id}
        await self._connection.execute(insert(self.table).values(row))
