"""Access events: a record of who inspected which run (PRD authorization table, §16, ADR-0013).

A permitted read of a run through the approvals or ops audience produces one access event. They are
operational access logs, not side-effect authorization events: separate from the authoritative
per-run HMAC chain (ADR-0015), written outside the request in batches, and allowed to be lost
(a full buffer, a failed write, a crash). Losing one never changes the answer to the read.

``AccessEventWriter`` buffers events in a bounded queue and flushes every second, or as soon as a
batch fills. It never raises to its caller; every event it cannot keep is counted in
``tether.access_events.dropped``. It is created on first use by ``writer_for`` and flushed by
``aclose`` when the application shuts down.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import State

from tether.core.ids import WorkspaceId
from tether.identity.principal import Principal
from tether.observability.logging import get_logger
from tether.storage.repos.access_events import AccessEventsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

RUN_INSPECTED = "run.inspect"
FLUSH_INTERVAL_SECONDS = 1.0
BATCH_SIZE = 100
CAPACITY = 1000

_log = get_logger(__name__)
_DROPPED = metrics.get_meter("tether").create_counter(
    "tether.access_events.dropped",
    unit="{event}",
    description="Access events not recorded (buffer full, write failed or shut down)",
)


@dataclass(frozen=True, slots=True)
class AccessEvent:
    workspace_id: str
    actor: str
    audience: str
    action: str
    target_run_id: uuid.UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "audience": self.audience,
            "action": self.action,
            "target_run_id": self.target_run_id,
            "details": self.details,
        }


class AccessEventWriter:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        batch_size: int = BATCH_SIZE,
        capacity: int = CAPACITY,
    ) -> None:
        self._engine = engine
        self._interval = flush_interval
        self._batch_size = batch_size
        self._capacity = capacity
        self._buffer: list[AccessEvent] = []
        self._flushing = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def record(self, event: AccessEvent) -> bool:
        """Queue ``event``; ``False`` (and a drop count) if it was not kept. Never raises."""
        try:
            if self._closed or len(self._buffer) >= self._capacity:
                _DROPPED.add(1)
                return False
            self._buffer.append(event)
            self._ensure_running()
            if len(self._buffer) >= self._batch_size:
                self._wake.set()
            return True
        except Exception:  # an access log must never break a read
            return False

    def _ensure_running(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._run())
        except RuntimeError:
            self._task = None  # no event loop yet: ``flush`` still writes what is buffered

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval)
            self._wake.clear()
            await self.flush()

    async def flush(self) -> None:
        """Write everything buffered now, one transaction per workspace. Never raises."""
        async with self._flushing:
            pending, self._buffer = self._buffer, []
            by_workspace: dict[str, list[AccessEvent]] = {}
            for event in pending:
                by_workspace.setdefault(event.workspace_id, []).append(event)
            for workspace_id, events in by_workspace.items():
                try:
                    scope = WorkspaceScope(WorkspaceId(workspace_id))
                    async with UnitOfWork(self._engine) as uow:
                        await uow.repository(AccessEventsRepository, scope).append_many(
                            [event.row() for event in events]
                        )
                except Exception as error:
                    _DROPPED.add(len(events))
                    _log.warning(
                        "access events not recorded",
                        workspace_id=workspace_id,
                        count=len(events),
                        error=type(error).__name__,
                    )

    async def aclose(self) -> None:
        """Stop the background flusher and write what is left."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.flush()


def writer_for(state: State) -> AccessEventWriter:
    """The application's writer, created on first use (``app.state.access_events``)."""
    writer: AccessEventWriter | None = getattr(state, "access_events", None)
    if writer is None:
        writer = AccessEventWriter(state.db_engine)
        state.access_events = writer
    return writer


def record_run_inspection(state: State, principal: Principal, run_id: uuid.UUID) -> None:
    """Record that ``principal`` inspected ``run_id``. Call only after the read is permitted."""
    try:
        writer_for(state).record(
            AccessEvent(
                workspace_id=principal.workspace_id,
                actor=principal.subject,
                audience=principal.audience,
                action=RUN_INSPECTED,
                target_run_id=run_id,
                details={"roles": list(principal.roles)},
            )
        )
    except Exception:  # an access log must never break a read
        _log.warning("access event not recorded", workspace_id=principal.workspace_id)
