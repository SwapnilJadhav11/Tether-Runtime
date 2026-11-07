"""Walking-skeleton slice: submit a run, lease its job, drive it to ``succeeded``, read it back.

Covers thin parts of S2.1 (lease/complete/release) and S2.8 (TX1 without audit, a minimal fenced
run driver). Identity comes from the dev/test-only request shortcut that S1.8 replaces.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.api.app import create_app
from tether.queue import postgres as queue
from tether.settings import Settings
from tether.storage.engine import create_database_engine
from tether.storage.repos.runs import JobsRepository
from tether.storage.scope import WorkspaceScope
from tether.worker.handlers import advance_run
from tether.worker.main import process_next_job, run_worker

SCOPE_A = WorkspaceScope("ws-a")
Client = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


def _settings(seeded: Any, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"env": "test", "worker_poll_interval_seconds": 0.05, **overrides}
    return Settings(database_url=SecretStr(seeded.db.app_dsn), **values)


@pytest.fixture
def client(seeded: Any) -> Client:
    """An API client with the app lifespan running; each use is a fresh app instance."""

    @asynccontextmanager
    async def open_client(**overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
        app = create_app(_settings(seeded, **overrides))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://tether"
            ) as http,
        ):
            yield http

    return open_client


@pytest_asyncio.fixture
async def engine(seeded: Any) -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(seeded.db.app_dsn)
    try:
        yield engine
    finally:
        await engine.dispose()


def submission(workspace: str = "ws-a", agent: str = "agent-1") -> dict[str, Any]:
    return {
        "dev_identity": {
            "workspace_id": workspace,
            "agent_id": agent,
            "requester": {"sub": "user-1", "roles": ["operator"]},
        },
        "task_input": {"message": "hello"},
    }


async def submit(client: Client) -> uuid.UUID:
    async with client() as http:
        response = await http.post("/v1/tasks/runs", json=submission())
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def read(client: Client, run_id: uuid.UUID, workspace: str = "ws-a") -> httpx.Response:
    async with client() as http:
        return await http.get(f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": workspace})


def query(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        cursor = conn.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def run_row(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    rows = query(
        seeded,
        "SELECT status, driver_generation, workspace_id, agent_id, requester_snapshot,"
        " task_input, deadline_at > created_at FROM tether.runs WHERE id = %s",
        run_id,
    )
    assert len(rows) == 1
    return rows[0]


def job_rows(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return query(
        seeded,
        "SELECT kind, state, attempts, workspace_id, run_after > clock_timestamp()"
        " FROM tether.jobs WHERE run_id = %s",
        run_id,
    )


# --- Submit (TX1 without audit) -----------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_persists_a_queued_run_and_one_ready_advance_run_job(
    client: Client, seeded: Any
) -> None:
    run_id = await submit(client)
    status, generation, workspace, agent, requester, task_input, has_deadline = run_row(
        seeded, run_id
    )
    assert (status, generation, workspace, agent) == ("queued", 0, "ws-a", "agent-1")
    assert requester == {"sub": "user-1", "roles": ["operator"]}
    assert task_input == {"message": "hello"}
    assert has_deadline
    assert job_rows(seeded, run_id) == [("advance_run", "ready", 0, "ws-a", False)]


@pytest.mark.asyncio
async def test_submit_writes_nothing_when_the_enqueue_fails(
    client: Client, seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EnqueueError(Exception):
        pass

    async def failing_add(self: JobsRepository, values: Any) -> None:
        raise EnqueueError

    monkeypatch.setattr(JobsRepository, "add", failing_add)
    before = query(seeded, "SELECT count(*) FROM tether.runs")
    with pytest.raises(EnqueueError):
        async with client() as http:
            await http.post("/v1/tasks/runs", json=submission())
    assert query(seeded, "SELECT count(*) FROM tether.runs") == before
    assert query(seeded, "SELECT count(*) FROM tether.jobs") == [(0,)]


@pytest.mark.asyncio
async def test_submit_for_an_unknown_agent_is_not_found(client: Client, seeded: Any) -> None:
    async with client() as http:
        response = await http.post("/v1/tasks/runs", json=submission(agent="nobody"))
    assert response.status_code == 404
    assert query(seeded, "SELECT count(*) FROM tether.jobs") == [(0,)]


@pytest.mark.asyncio
async def test_dev_identity_shortcut_is_refused_in_prod(client: Client, seeded: Any) -> None:
    before = query(seeded, "SELECT count(*) FROM tether.runs")
    async with client(env="prod") as http:
        submitted = await http.post("/v1/tasks/runs", json=submission())
        fetched = await http.get(
            f"/v1/tasks/runs/{seeded.run_a}", params={"dev_workspace_id": "ws-a"}
        )
    assert submitted.status_code == 404
    assert fetched.status_code == 404
    assert query(seeded, "SELECT count(*) FROM tether.runs") == before


# --- Read ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_is_workspace_scoped(client: Client, seeded: Any) -> None:
    own = await read(client, seeded.run_a)
    other = await read(client, seeded.run_a, workspace="ws-b")
    missing = await read(client, uuid.uuid4())
    assert own.status_code == 200
    assert own.json()["status"] == "queued"
    assert other.status_code == 404
    assert missing.status_code == 404


# --- End to end ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submitted_run_is_driven_to_succeeded_by_a_separate_worker(
    client: Client, engine: AsyncEngine, seeded: Any
) -> None:
    """The API instance is gone before the worker starts: all hand-off state is in Postgres."""
    run_id = await submit(client)

    assert await process_next_job(engine, _settings(seeded)) is True
    assert await process_next_job(engine, _settings(seeded)) is False

    response = await read(client, run_id)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(run_id)
    assert body["status"] == "succeeded"
    assert run_row(seeded, run_id)[:2] == ("succeeded", 1)
    assert job_rows(seeded, run_id) == [("advance_run", "done", 1, "ws-a", False)]


@pytest.mark.asyncio
async def test_worker_process_loop_completes_a_submitted_run(
    client: Client, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(client)
    stop = asyncio.Event()
    settings = _settings(seeded, worker_heartbeat_file=tmp_path / "beat")
    worker = asyncio.create_task(run_worker(settings, stop))
    try:
        for _ in range(200):
            if run_row(seeded, run_id)[0] == "succeeded":
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=15)
    assert run_row(seeded, run_id)[:2] == ("succeeded", 1)
    assert job_rows(seeded, run_id)[0][1] == "done"


# --- Queue and driver safety --------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workers_racing_lease_the_job_once(
    client: Client, seeded: Any, engine: AsyncEngine
) -> None:
    run_id = await submit(client)
    other = create_database_engine(seeded.db.app_dsn)
    try:
        results = await asyncio.gather(
            process_next_job(engine, _settings(seeded)),
            process_next_job(other, _settings(seeded)),
        )
    finally:
        await other.dispose()
    assert sorted(results) == [False, True]
    assert run_row(seeded, run_id)[:2] == ("succeeded", 1)
    assert job_rows(seeded, run_id) == [("advance_run", "done", 1, "ws-a", False)]


@pytest.mark.asyncio
async def test_contended_run_lock_requeues_the_job_without_consuming_an_attempt(
    client: Client, seeded: Any, engine: AsyncEngine
) -> None:
    run_id = await submit(client)
    with psycopg.connect(seeded.db.app_dsn, autocommit=True) as holder:
        holder.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (advance_run.run_lock_name(run_id),)
        )
        assert await process_next_job(engine, _settings(seeded)) is True
    assert run_row(seeded, run_id)[:2] == ("queued", 0)
    # Back to ready with a short delay, and the attempt is refunded.
    assert job_rows(seeded, run_id) == [("advance_run", "ready", 0, "ws-a", True)]


@pytest.mark.asyncio
async def test_a_stale_fencing_token_cannot_complete_the_run(
    client: Client, seeded: Any, engine: AsyncEngine
) -> None:
    run_id = await submit(client)
    generation = await advance_run.start(engine, SCOPE_A, run_id)
    assert generation == 1
    # A newer driver takes over (the zombie-driver case in ADR-0005).
    assert await advance_run.start(engine, SCOPE_A, run_id) == 2
    assert await advance_run.finish(engine, SCOPE_A, run_id, generation) is False
    assert run_row(seeded, run_id)[:2] == ("running", 2)
    assert await advance_run.finish(engine, SCOPE_A, run_id, 2) is True
    assert run_row(seeded, run_id)[:2] == ("succeeded", 2)


@pytest.mark.asyncio
async def test_a_terminal_run_is_not_restarted_by_a_redelivered_job(
    client: Client, seeded: Any, engine: AsyncEngine
) -> None:
    run_id = await submit(client)
    assert await process_next_job(engine, _settings(seeded)) is True
    assert await advance_run.start(engine, SCOPE_A, run_id) is None
    assert run_row(seeded, run_id)[:2] == ("succeeded", 1)


@pytest.mark.asyncio
async def test_an_expired_lease_is_reclaimed_after_a_worker_crash(
    client: Client, seeded: Any, engine: AsyncEngine
) -> None:
    run_id = await submit(client)
    crashed = await queue.lease_one(engine, kinds=("advance_run",), lease_seconds=60)
    assert crashed is not None
    # Still leased: no other worker may take it.
    assert await process_next_job(engine, _settings(seeded)) is False
    query(seeded, "UPDATE tether.jobs SET lease_expires_at = clock_timestamp() - interval '1 s'")
    assert await process_next_job(engine, _settings(seeded)) is True
    assert run_row(seeded, run_id)[:2] == ("succeeded", 1)
    assert job_rows(seeded, run_id) == [("advance_run", "done", 2, "ws-a", False)]
    # The crashed worker's lease token is stale: it cannot complete the job.
    assert await queue.complete(engine, crashed) is False
