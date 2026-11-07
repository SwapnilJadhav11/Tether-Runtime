"""Workspace-scoped repositories for runs and their jobs."""

from __future__ import annotations

import uuid

from sqlalchemy import update

from tether.core.statuses import RunStatus
from tether.storage import tables
from tether.storage.repos.base import ScopedRepository


class RunsRepository(ScopedRepository):
    table = tables.runs

    async def claim_driver_generation(self, run_id: uuid.UUID) -> int | None:
        """Mark a non-terminal run ``running`` and take the next fencing token (ADR-0005).

        Returns ``None`` if the run is not found in scope or is no longer ``queued``/``running``.
        """
        runs = self.table
        statement = (
            update(runs)
            .where(
                runs.c.workspace_id == self.scope.workspace_id,
                runs.c.id == run_id,
                runs.c.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
            )
            .values(status=RunStatus.RUNNING, driver_generation=runs.c.driver_generation + 1)
            .returning(runs.c.driver_generation)
        )
        result = await self._connection.execute(statement)
        generation = result.scalar_one_or_none()
        return None if generation is None else int(generation)

    async def transition(
        self, run_id: uuid.UUID, *, expected: RunStatus, new: RunStatus, generation: int
    ) -> bool:
        """Conditional status change, refused unless ``generation`` is still current."""
        runs = self.table
        statement = (
            update(runs)
            .where(
                runs.c.workspace_id == self.scope.workspace_id,
                runs.c.id == run_id,
                runs.c.status == expected,
                runs.c.driver_generation == generation,
            )
            .values(status=new)
            .returning(runs.c.id)
        )
        result = await self._connection.execute(statement)
        return result.scalar_one_or_none() is not None


class JobsRepository(ScopedRepository):
    table = tables.jobs
