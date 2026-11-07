"""S1.5: Unit of Work transaction boundaries and workspace-scoped repositories (AD-04, ADR-0013)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tether.core.errors import ScopeViolationError, UnitOfWorkError
from tether.storage import tables
from tether.storage.engine import create_database_engine
from tether.storage.repos.base import ScopedRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

WS_A, WS_B = "ws-a", "ws-b"
SCOPE_A = WorkspaceScope("ws-a")
SCOPE_B = WorkspaceScope("ws-b")


class RunsProbe(ScopedRepository):
    """A test-only repository over ``runs``; concrete repositories arrive in later steps."""

    table = tables.runs


@pytest_asyncio.fixture
async def engine(seeded: Any) -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(seeded.db.app_dsn)
    try:
        yield engine
    finally:
        await engine.dispose()


def new_run(**overrides: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "agent_id": "agent-1",
        "status": "queued",
        "requester_snapshot": {"sub": "user-1", "roles": []},
        "task_input": {"message": "probe"},
        "deadline_at": datetime.now(UTC) + timedelta(hours=1),
        **overrides,
    }


@pytest.mark.asyncio
async def test_commits_when_the_block_succeeds(engine: AsyncEngine) -> None:
    run = new_run()
    async with UnitOfWork(engine) as uow:
        await uow.repository(RunsProbe, SCOPE_A).add(run)
    async with UnitOfWork(engine) as uow:
        found = await uow.repository(RunsProbe, SCOPE_A).get(run["id"])
    assert found is not None
    assert found["workspace_id"] == WS_A


@pytest.mark.asyncio
async def test_rolls_back_when_the_block_raises(engine: AsyncEngine) -> None:
    run = new_run()

    class BoomError(Exception):
        pass

    async def add_then_fail() -> None:
        async with UnitOfWork(engine) as uow:
            await uow.repository(RunsProbe, SCOPE_A).add(run)
            raise BoomError

    with pytest.raises(BoomError):
        await add_then_fail()
    async with UnitOfWork(engine) as uow:
        assert await uow.repository(RunsProbe, SCOPE_A).get(run["id"]) is None


@pytest.mark.asyncio
async def test_cross_workspace_lookup_returns_not_found(engine: AsyncEngine, seeded: Any) -> None:
    async with UnitOfWork(engine) as uow:
        assert await uow.repository(RunsProbe, SCOPE_A).get(seeded.run_a) is not None
        assert await uow.repository(RunsProbe, SCOPE_B).get(seeded.run_a) is None


@pytest.mark.asyncio
async def test_add_stamps_the_scope_workspace(engine: AsyncEngine) -> None:
    run = new_run()
    async with UnitOfWork(engine) as uow:
        await uow.repository(RunsProbe, SCOPE_B).add(run)
        found = await uow.repository(RunsProbe, SCOPE_B).get(run["id"])
    assert found is not None
    assert found["workspace_id"] == WS_B


@pytest.mark.asyncio
async def test_add_refuses_a_row_for_another_workspace(engine: AsyncEngine) -> None:
    async with UnitOfWork(engine) as uow:
        with pytest.raises(ScopeViolationError):
            await uow.repository(RunsProbe, SCOPE_A).add(new_run(workspace_id=WS_B))


@pytest.mark.asyncio
async def test_repositories_cannot_be_built_without_a_scope(engine: AsyncEngine) -> None:
    async with UnitOfWork(engine) as uow:
        with pytest.raises(TypeError):
            uow.repository(RunsProbe, None)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            uow.repository(RunsProbe, "ws-a")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_repositories_are_unavailable_outside_an_active_unit_of_work(
    engine: AsyncEngine,
) -> None:
    uow = UnitOfWork(engine)
    with pytest.raises(UnitOfWorkError):
        uow.repository(RunsProbe, SCOPE_A)
    async with uow:
        pass
    with pytest.raises(UnitOfWorkError):
        uow.repository(RunsProbe, SCOPE_A)


@pytest.mark.asyncio
async def test_a_unit_of_work_cannot_be_entered_twice(engine: AsyncEngine) -> None:
    uow = UnitOfWork(engine)
    async with uow:
        with pytest.raises(UnitOfWorkError):
            await uow.__aenter__()


@pytest.mark.asyncio
async def test_concurrent_entry_is_refused_without_leaking_a_connection(
    engine: AsyncEngine,
) -> None:
    """F3: the re-entry guard is claimed before the first await."""
    uow = UnitOfWork(engine)
    results = await asyncio.gather(uow.__aenter__(), uow.__aenter__(), return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], UnitOfWorkError)
    await uow.__aexit__(None, None, None)
    assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cleanup_completes_when_the_task_keeps_being_cancelled(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: rollback and close finish even if cancellation arrives during cleanup."""
    run = new_run()
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    real_rollback, real_close = AsyncConnection.rollback, AsyncConnection.close

    async def slow_rollback(self: AsyncConnection) -> None:
        cleanup_started.set()
        await asyncio.sleep(0.2)
        await real_rollback(self)

    async def slow_close(self: AsyncConnection) -> None:
        await asyncio.sleep(0.2)
        await real_close(self)

    monkeypatch.setattr(AsyncConnection, "rollback", slow_rollback)
    monkeypatch.setattr(AsyncConnection, "close", slow_close)

    async def work() -> None:
        async with UnitOfWork(engine) as uow:
            await uow.repository(RunsProbe, SCOPE_A).add(run)
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    await cleanup_started.wait()
    while not task.done():  # keep cancelling while rollback and close are in progress
        task.cancel()
        await asyncio.sleep(0.05)
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(1.0)  # the shielded cleanup runs to completion
    assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    monkeypatch.undo()
    async with UnitOfWork(engine) as uow:
        assert await uow.repository(RunsProbe, SCOPE_A).get(run["id"]) is None
