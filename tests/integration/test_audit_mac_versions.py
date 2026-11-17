"""W2.4: a versioned audit MAC that covers ``created_at`` (ADR-0015 rule 1, AD-12, T-13).

``mac_version`` says how one event's own MAC is computed; a run's chain may mix versions.

- Version 1 (every event written before migration ``0003``): the legacy record, without time.
  Those events are never rewritten or re-MACed; their timestamps were never covered.
- Version 2 (every new append): the version-1 fields plus ``mac_version`` and ``created_at``,
  where ``created_at`` is read from the database clock inside the appending transaction, stored
  explicitly and MACed as the same instant (UTC, microseconds, ``Z``).
- Any other version fails verification closed.

The legacy and v2 MACs are recomputed here independently of ``tether.audit.chain``, so a change
to either algorithm in the code cannot pass unnoticed.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from psycopg import errors
from psycopg.types.json import Jsonb
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.audit.chain import verify_run
from tether.cli.main import MIGRATIONS_LOCATION, upgrade_to_head
from tether.core.canonical import canonicalize
from tether.core.ids import WorkspaceId
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyFamily, KeyRing
from tether.storage.engine import create_database_engine
from tether.storage.repos.audit import AuditRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

WS = "ws-a"
SCOPE = WorkspaceScope(WorkspaceId(WS))
KEYS = KeyRing(EnvSecretsProvider())


def audit_key() -> tuple[str, bytes]:
    key_id = KEYS.active_key_id(KeyFamily.AUDIT)
    return key_id, KEYS.key(KeyFamily.AUDIT, key_id).secret.reveal()


def legacy_record(run_id: uuid.UUID, seq: int, dedupe: str, prev_mac: str | None) -> dict[str, Any]:
    """The exact version-1 MAC record as written before W2.4 (no time, no version)."""
    return {
        "workspace_id": WS,
        "run_id": str(run_id),
        "seq": seq,
        "dedupe_key": dedupe,
        "event_type": "PROBE",
        "actor": None,
        "tool": None,
        "payload": {"n": seq},
        "policy_version": None,
        "prev_mac": prev_mac,
    }


def hmac_hex(key: bytes, record: dict[str, Any]) -> str:
    return hmac.new(key, canonicalize(record, allow_floats=True), hashlib.sha256).hexdigest()


def utc_text(value: datetime) -> str:
    """The v2 representation of a stored ``created_at``: UTC, six fractional digits, ``Z``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def seed_v1(db: Any, run_id: uuid.UUID, count: int, *, with_version: bool = True) -> list[str]:
    """Write a valid version-1 chain of ``count`` events directly, as the old code did."""
    key_id, key = audit_key()
    macs: list[str] = []
    prev_mac: str | None = None
    with psycopg.connect(db.migrator_dsn) as conn:
        for seq in range(1, count + 1):
            dedupe = f"legacy-{seq}"
            mac = hmac_hex(key, legacy_record(run_id, seq, dedupe, prev_mac))
            columns = "workspace_id, run_id, seq, dedupe_key, event_type, payload_masked,"
            columns += " prev_mac, mac, key_id"
            values = [WS, run_id, seq, dedupe, "PROBE", Jsonb({"n": seq}), prev_mac, mac, key_id]
            if with_version:
                columns += ", mac_version"
                values.append(1)
            placeholders = ", ".join(["%s"] * len(values))
            conn.execute(
                f"INSERT INTO tether.audit_events ({columns}) VALUES ({placeholders})",  # noqa: S608
                values,
            )
            macs.append(mac)
            prev_mac = mac
        conn.execute(
            "INSERT INTO tether.audit_heads (run_id, workspace_id, seq, last_mac, key_id)"
            " VALUES (%s, %s, %s, %s, %s)",
            (run_id, WS, count, prev_mac, key_id),
        )
    return macs


def rows(db: Any, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with psycopg.connect(db.migrator_dsn) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM tether.audit_events WHERE run_id = %s ORDER BY seq", (run_id,)
        )
        names = [column.name for column in cursor.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def head(db: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    with psycopg.connect(db.migrator_dsn) as conn:
        found = conn.execute(
            "SELECT seq, last_mac, key_id FROM tether.audit_heads WHERE run_id = %s", (run_id,)
        ).fetchone()
    assert found is not None
    return tuple(found)


def tamper(db: Any, statement: str, *params: Any) -> None:
    """A privileged edit, as someone with database access would make it."""
    with psycopg.connect(db.superuser_dsn(db.dbname)) as admin:
        admin.execute(statement, params)


def expected_v2_mac(row: dict[str, Any]) -> str:
    """The v2 MAC recomputed from the stored row alone."""
    _, key = audit_key()
    record = {
        "workspace_id": row["workspace_id"],
        "run_id": str(row["run_id"]),
        "seq": row["seq"],
        "dedupe_key": row["dedupe_key"],
        "event_type": row["event_type"],
        "actor": row["actor"],
        "tool": row["tool"],
        "payload": row["payload_masked"],
        "policy_version": row["policy_version"],
        "prev_mac": row["prev_mac"],
        "mac_version": 2,
        "created_at": utc_text(row["created_at"]),
    }
    return hmac_hex(key, record)


@pytest_asyncio.fixture
async def engine(seeded: Any) -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(seeded.db.app_dsn)
    try:
        yield engine
    finally:
        await engine.dispose()


async def append(
    engine: AsyncEngine, run_id: uuid.UUID, dedupe: str, payload: dict[str, Any]
) -> Any:
    async with UnitOfWork(engine) as uow:
        return await uow.repository(AuditRepository, SCOPE).append(
            KEYS, run_id=run_id, dedupe_key=dedupe, event_type="PROBE", payload=payload
        )


async def verify(engine: AsyncEngine, run_id: uuid.UUID) -> bool:
    async with UnitOfWork(engine) as uow:
        return await verify_run(uow.repository(AuditRepository, SCOPE), KEYS, run_id)


# --- B. new events are version 2 and MAC their stored timestamp ----------------------------


@pytest.mark.asyncio
async def test_b_new_events_are_version_2_and_their_mac_covers_the_stored_created_at(
    engine: AsyncEngine, seeded: Any
) -> None:
    for n in range(3):
        record = await append(engine, seeded.run_a, f"e{n}", {"n": n})
        assert record.mac_version == 2
    stored = rows(seeded.db, seeded.run_a)
    assert [row["mac_version"] for row in stored] == [2, 2, 2]
    for row in stored:
        # The MAC is over exactly the instant that was persisted, recomputed from the row.
        assert row["mac"] == expected_v2_mac(row)
    assert stored[0]["created_at"] <= stored[1]["created_at"] <= stored[2]["created_at"]
    assert await verify(engine, seeded.run_a)


# --- A. T-13: a changed timestamp is detected -----------------------------------------------


@pytest.mark.parametrize("change", ["1 microsecond", "1 hour", "-1 day"])
@pytest.mark.asyncio
async def test_a_t13_a_changed_created_at_on_a_v2_event_fails_verification(
    engine: AsyncEngine, seeded: Any, change: str
) -> None:
    for n in range(3):
        await append(engine, seeded.run_a, f"e{n}", {"n": n})
    assert await verify(engine, seeded.run_a)
    tamper(
        seeded.db,
        "UPDATE tether.audit_events SET created_at = created_at + %s::interval"
        " WHERE run_id = %s AND seq = 2",
        change,
        seeded.run_a,
    )
    assert not await verify(engine, seeded.run_a)


# --- C. legacy version-1 chains still verify ------------------------------------------------


@pytest.mark.asyncio
async def test_c_a_legacy_v1_chain_still_verifies(engine: AsyncEngine, seeded: Any) -> None:
    seed_v1(seeded.db, seeded.run_a, 3)
    assert await verify(engine, seeded.run_a)
    # Its timestamps were never covered: changing one leaves a v1 chain valid (stated limit).
    tamper(
        seeded.db,
        "UPDATE tether.audit_events SET created_at = created_at - interval '1 day'"
        " WHERE run_id = %s AND seq = 2",
        seeded.run_a,
    )
    assert await verify(engine, seeded.run_a)
    tamper(
        seeded.db,
        "UPDATE tether.audit_events SET payload_masked = '{\"n\": 9}'"
        " WHERE run_id = %s AND seq = 2",
        seeded.run_a,
    )
    assert not await verify(engine, seeded.run_a)


# --- D and G. mixed chains continue the same chain ------------------------------------------


@pytest.mark.asyncio
async def test_d_g_a_v1_chain_continues_with_v2_events_without_a_fork(
    engine: AsyncEngine, seeded: Any
) -> None:
    legacy = seed_v1(seeded.db, seeded.run_a, 2)
    key_id = head(seeded.db, seeded.run_a)[2]
    third = await append(engine, seeded.run_a, "new-3", {"n": 3})
    fourth = await append(engine, seeded.run_a, "new-4", {"n": 4})
    stored = rows(seeded.db, seeded.run_a)
    assert [(r["seq"], r["mac_version"]) for r in stored] == [(1, 1), (2, 1), (3, 2), (4, 2)]
    assert [r["mac"] for r in stored[:2]] == legacy  # the v1 events are untouched
    assert stored[2]["prev_mac"] == legacy[1]  # the v2 event links to the real v1 MAC
    assert stored[3]["prev_mac"] == third.mac
    assert head(seeded.db, seeded.run_a) == (4, fourth.mac, key_id)  # same head, same key
    assert await verify(engine, seeded.run_a)


# --- E. unsupported or altered versions fail closed -----------------------------------------


@pytest.mark.parametrize(("seq", "version"), [(1, 3), (3, 3), (3, 1), (1, 2)])
@pytest.mark.asyncio
async def test_e_an_unsupported_or_altered_mac_version_fails_verification(
    engine: AsyncEngine, seeded: Any, seq: int, version: int
) -> None:
    """3 is unknown; v2 → 1 would drop the timestamp from the check (a downgrade); v1 → 2
    claims coverage the event never had. All three fail."""
    seed_v1(seeded.db, seeded.run_a, 2)
    await append(engine, seeded.run_a, "new-3", {"n": 3})
    assert await verify(engine, seeded.run_a)
    tamper(
        seeded.db,
        "UPDATE tether.audit_events SET mac_version = %s WHERE run_id = %s AND seq = %s",
        version,
        seeded.run_a,
        seq,
    )
    assert not await verify(engine, seeded.run_a)


# --- H. replay adopts the stored event, whatever its version --------------------------------


@pytest.mark.asyncio
async def test_h_replaying_a_v1_event_adopts_it_and_never_rewrites_it_as_v2(
    engine: AsyncEngine, seeded: Any
) -> None:
    seed_v1(seeded.db, seeded.run_a, 2)
    before = rows(seeded.db, seeded.run_a)
    again = await append(engine, seeded.run_a, "legacy-2", {"n": 99})
    assert again.created is False
    assert (again.seq, again.mac_version, again.mac, again.payload) == (
        2,
        1,
        before[1]["mac"],
        {"n": 2},
    )
    assert rows(seeded.db, seeded.run_a) == before
    assert head(seeded.db, seeded.run_a)[0] == 2
    assert await verify(engine, seeded.run_a)


# --- I. a failed append leaves nothing behind -----------------------------------------------


@pytest.mark.asyncio
async def test_i_a_rolled_back_append_leaves_no_event_and_no_head_change(
    engine: AsyncEngine, seeded: Any
) -> None:
    first = await append(engine, seeded.run_a, "kept", {"n": 1})

    class BoomError(Exception):
        pass

    async def append_then_fail() -> None:
        async with UnitOfWork(engine) as uow:
            await uow.repository(AuditRepository, SCOPE).append(
                KEYS, run_id=seeded.run_a, dedupe_key="lost", event_type="PROBE", payload={}
            )
            raise BoomError

    with pytest.raises(BoomError):
        await append_then_fail()
    assert [r["dedupe_key"] for r in rows(seeded.db, seeded.run_a)] == ["kept"]
    assert head(seeded.db, seeded.run_a)[:2] == (1, first.mac)
    assert await verify(engine, seeded.run_a)


# --- the migration: existing rows become v1, new writes must say their version --------------


def _alembic(dsn: str) -> Config:
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    config.attributes["database_url"] = dsn
    return config


@pytest.mark.asyncio
async def test_the_migration_marks_existing_events_v1_and_they_still_verify(
    seeded: Any,
) -> None:
    command.downgrade(_alembic(seeded.db.migrator_dsn), "0002")
    legacy = seed_v1(seeded.db, seeded.run_a, 2, with_version=False)
    upgrade_to_head(seeded.db.migrator_dsn)
    stored = rows(seeded.db, seeded.run_a)
    assert [(r["mac_version"], r["mac"]) for r in stored] == [(1, legacy[0]), (1, legacy[1])]
    engine = create_database_engine(seeded.db.app_dsn)
    try:
        assert await verify(engine, seeded.run_a)
        record = await append(engine, seeded.run_a, "after-migration", {"n": 3})
        assert (record.seq, record.mac_version, record.prev_mac) == (3, 2, legacy[1])
        assert await verify(engine, seeded.run_a)
    finally:
        await engine.dispose()


def test_a_write_that_does_not_state_its_mac_version_is_refused(seeded: Any) -> None:
    """No default: a writer that forgets the version cannot silently produce a v1 event."""
    with (
        psycopg.connect(seeded.db.migrator_dsn) as conn,
        pytest.raises(errors.NotNullViolation),
    ):
        conn.execute(
            "INSERT INTO tether.audit_events"
            " (workspace_id, run_id, seq, dedupe_key, event_type, payload_masked, mac, key_id)"
            " VALUES (%s, %s, 1, 'x', 'PROBE', '{}', %s, 'K1')",
            (WS, seeded.run_a, "a" * 64),
        )


def test_mac_versions_must_be_positive(seeded: Any) -> None:
    with (
        psycopg.connect(seeded.db.migrator_dsn) as conn,
        pytest.raises(errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO tether.audit_events (workspace_id, run_id, seq, dedupe_key, event_type,"
            " payload_masked, mac, key_id, mac_version)"
            " VALUES (%s, %s, 1, 'x', 'PROBE', '{}', %s, 'K1', 0)",
            (WS, seeded.run_a, "a" * 64),
        )


def test_utc_text_is_the_documented_representation() -> None:
    assert utc_text(datetime(2026, 11, 14, 9, 5, 7, 1200, tzinfo=UTC)) == (
        "2026-11-14T09:05:07.001200Z"
    )
