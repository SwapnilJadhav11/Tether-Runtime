"""Access events: operational logs of who inspected which run (PRD authorization table, §16).

Not part of the per-run HMAC chain (ADR-0015) and append-only for the application role. They are
written outside the request, in batches, by ``tether.audit.access_events``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import insert

from tether.core.errors import ScopeViolationError
from tether.storage import tables
from tether.storage.repos.base import ScopedRepository


class AccessEventsRepository(ScopedRepository):
    table = tables.access_events

    async def append_many(self, events: Sequence[dict[str, Any]]) -> None:
        """Insert ``events`` (each with ``actor``, ``audience``, ``action``, ``details`` ...)."""
        rows = []
        for event in events:
            if event.get("workspace_id", self._scope.workspace_id) != self._scope.workspace_id:
                raise ScopeViolationError("refusing to write an access event for another workspace")
            rows.append({"id": uuid.uuid4(), **event, "workspace_id": self._scope.workspace_id})
        if rows:
            await self._connection.execute(insert(self.table), rows)
