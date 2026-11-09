"""Unit of Work: one database transaction per catalogue transaction (TX1-TX12).

The transaction boundary belongs to the code that performs a TX (an API handler or service, or
a worker job handler), never to a repository. Entering the Unit of Work opens a connection and
begins a transaction; leaving commits it, or rolls it back if the block raised. Callers reach
data only through workspace-scoped repositories bound to that transaction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tether.core.errors import UnitOfWorkError
from tether.storage.repos.base import ScopedRepository
from tether.storage.scope import WorkspaceScope

# Cleanup tasks still finishing after their caller was cancelled (kept alive until done).
_PENDING_CLEANUPS: set[asyncio.Task[None]] = set()


async def _finish(connection: AsyncConnection, *, commit: bool) -> None:
    try:
        if commit:
            await connection.commit()
        else:
            await connection.rollback()
    finally:
        await connection.close()


class UnitOfWork:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: AsyncConnection | None = None
        self._active = False
        self._after_commit: list[Callable[[], None]] = []

    def on_commit(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after this transaction commits; never after a rollback.

        For best-effort side channels only (metrics, logs): a callback's failure is ignored and
        cannot affect the committed transaction or the caller.
        """
        if not self._active:
            raise UnitOfWorkError("on_commit is only available inside an active Unit of Work")
        self._after_commit.append(callback)

    async def __aenter__(self) -> Self:
        # Claim the guard before the first await, so concurrent entry cannot open two
        # connections on one instance.
        if self._active:
            raise UnitOfWorkError("this Unit of Work is already active")
        self._active = True
        try:
            connection = await self._engine.connect()
            try:
                await connection.begin()
            except BaseException:
                await connection.close()
                raise
        except BaseException:
            self._active = False
            raise
        self._connection = connection
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection, self._connection = self._connection, None
        self._active = False
        callbacks, self._after_commit = self._after_commit, []
        if connection is None:
            return
        # Commit/rollback and close run in their own task, shielded from cancellation of the
        # caller: if the caller is cancelled again during cleanup, the cancellation still
        # propagates to it, but the connection is finished and returned to the pool.
        cleanup = asyncio.ensure_future(_finish(connection, commit=exc_type is None))
        _PENDING_CLEANUPS.add(cleanup)
        cleanup.add_done_callback(_PENDING_CLEANUPS.discard)
        await asyncio.shield(cleanup)
        if exc_type is not None:
            return
        # Committed (a failed commit raised above): run the best-effort callbacks.
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: S112 - telemetry must never affect a committed transaction
                continue

    def repository[R: ScopedRepository](self, repository: type[R], scope: WorkspaceScope) -> R:
        """A repository bound to this transaction and limited to ``scope``."""
        if self._connection is None:
            raise UnitOfWorkError("repositories are only available inside an active Unit of Work")
        return repository(self._connection, scope)
