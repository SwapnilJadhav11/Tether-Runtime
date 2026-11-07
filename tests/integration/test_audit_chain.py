"""MVP M4: per-run HMAC audit chain written inside the caller's transaction (ADR-0015)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.audit.chain import verify_run
from tether.core.ids import WorkspaceId
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyRing
from tether.storage.engine import create_database_engine
from tether.storage.repos.audit import AuditRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

SCOPE_A = WorkspaceScope(WorkspaceId("ws-a"))
KEYS = KeyRing(EnvSecretsProvider())


@pytest_asyncio.fixture
async def engine(seeded: Any) -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(seeded.db.app_dsn)
    try:
        yield engine
    finally:
        await engine.dispose()


async def append(engine: AsyncEngine, run_id: Any, dedupe: str, payload: dict[str, Any]) -> Any:
    async with UnitOfWork(engine) as uow:
        return await uow.repository(AuditRepository, SCOPE_A).append(
            KEYS, run_id=run_id, dedupe_key=dedupe, event_type="PROBE", payload=payload
        )


async def verify(engine: AsyncEngine, run_id: Any) -> bool:
    async with UnitOfWork(engine) as uow:
        return await verify_run(uow.repository(AuditRepository, SCOPE_A), KEYS, run_id)


@pytest.mark.asyncio
async def test_events_form_a_verifiable_chain(engine: AsyncEngine, seeded: Any) -> None:
    first = await append(engine, seeded.run_a, "one", {"n": 1})
    second = await append(engine, seeded.run_a, "two", {"n": 2})
    assert (first.seq, second.seq) == (1, 2)
    assert second.prev_mac == first.mac
    assert await verify(engine, seeded.run_a)


@pytest.mark.asyncio
async def test_a_repeated_dedupe_key_returns_the_persisted_event(
    engine: AsyncEngine, seeded: Any
) -> None:
    original = await append(engine, seeded.run_a, "decision-0", {"decision": "allow"})
    again = await append(engine, seeded.run_a, "decision-0", {"decision": "deny"})
    assert again.created is False
    assert (again.seq, again.payload) == (original.seq, {"decision": "allow"})


@pytest.mark.asyncio
async def test_tampering_is_detected(engine: AsyncEngine, seeded: Any) -> None:
    await append(engine, seeded.run_a, "one", {"n": 1})
    await append(engine, seeded.run_a, "two", {"n": 2})
    with psycopg.connect(seeded.db.superuser_dsn(seeded.db.dbname)) as admin:
        admin.execute("UPDATE tether.audit_events SET payload_masked = '{\"n\": 9}' WHERE seq = 1")
    assert not await verify(engine, seeded.run_a)


@pytest.mark.asyncio
async def test_concurrent_appends_get_distinct_sequence_numbers(
    engine: AsyncEngine, seeded: Any
) -> None:
    events = await asyncio.gather(
        *(append(engine, seeded.run_a, f"e{i}", {"i": i}) for i in range(5))
    )
    assert sorted(event.seq for event in events) == [1, 2, 3, 4, 5]
    assert await verify(engine, seeded.run_a)


@pytest.mark.asyncio
async def test_an_event_rolls_back_with_its_transaction(engine: AsyncEngine, seeded: Any) -> None:
    class BoomError(Exception):
        pass

    async def append_then_fail() -> None:
        async with UnitOfWork(engine) as uow:
            await uow.repository(AuditRepository, SCOPE_A).append(
                KEYS, run_id=seeded.run_a, dedupe_key="x", event_type="PROBE", payload={}
            )
            raise BoomError

    with pytest.raises(BoomError):
        await append_then_fail()
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM tether.audit_events").fetchone() == (0,)
