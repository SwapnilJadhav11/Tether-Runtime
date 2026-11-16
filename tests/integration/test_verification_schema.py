"""W5.1 migration ``0005`` (ADR-0019 rules 9-12): verification columns, resolutions, job kind.

Existing entries are not rewritten (they read zero attempts and no evidence); a verified
resolution needs its resolver and evidence; the app role may update the new columns and still
has no DELETE; the downgrade restores ``0004`` and is refused while verified state exists.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors
from psycopg.types.json import Jsonb
from sqlalchemy.exc import IntegrityError

from tether.cli.main import MIGRATIONS_LOCATION, upgrade_to_head

WS = "ws-a"


def _alembic(dsn: str) -> Config:
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    config.attributes["database_url"] = dsn
    return config


def _columns(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        found = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'tether' AND table_name = 'ledger_entries'"
        ).fetchall()
    return {name for (name,) in found}


def test_w5_1_existing_entries_read_zero_attempts_and_no_evidence(seeded: Any, rows: Any) -> None:
    command.downgrade(_alembic(seeded.db.migrator_dsn), "0004")
    entry = rows.child("ledger_entries", WS, seeded.run_a, state="indeterminate")
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        rows.insert(conn, "ledger_entries", entry)
    upgrade_to_head(seeded.db.migrator_dsn)
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        found = conn.execute(
            "SELECT verification_attempts, verification_evidence_masked, resolution"
            " FROM tether.ledger_entries WHERE id = %s",
            (entry["id"],),
        ).fetchall()
    assert found == [(0, None, None)]


def test_w5_1_downgrade_restores_0004_and_upgrade_returns(seeded: Any) -> None:
    dsn = seeded.db.migrator_dsn
    assert {"verification_attempts", "verification_evidence_masked"} <= _columns(dsn)
    command.downgrade(_alembic(dsn), "0004")
    assert not {"verification_attempts", "verification_evidence_masked"} & _columns(dsn)
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM tether.alembic_version").fetchall() == [
            ("0004",)
        ]
    upgrade_to_head(dsn)
    assert {"verification_attempts", "verification_evidence_masked"} <= _columns(dsn)


def test_w5_1_the_0004_constraints_refuse_the_new_values_after_a_downgrade(
    seeded: Any, rows: Any
) -> None:
    command.downgrade(_alembic(seeded.db.migrator_dsn), "0004")
    with psycopg.connect(seeded.db.migrator_dsn) as conn, pytest.raises(errors.CheckViolation):
        rows.insert(conn, "jobs", rows.child("jobs", WS, seeded.run_a, kind="verify_outcome"))


@pytest.mark.parametrize(
    ("table", "overrides"),
    [
        (
            "ledger_entries",
            {
                "state": "completed",
                "resolution": "verified_applied",
                "resolved_by": "system:verification",
                "verification_attempts": 1,
                "verification_evidence_masked": Jsonb({"found": True}),
            },
        ),
        ("jobs", {"kind": "verify_outcome"}),
    ],
)
def test_w5_1_a_downgrade_is_refused_while_verification_state_exists(
    seeded: Any, rows: Any, table: str, overrides: dict[str, Any]
) -> None:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        rows.insert(conn, table, rows.child(table, WS, seeded.run_a, **overrides))
    with pytest.raises(IntegrityError) as refused:
        command.downgrade(_alembic(seeded.db.migrator_dsn), "0004")
    assert isinstance(refused.value.orig, errors.CheckViolation)
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        assert conn.execute("SELECT version_num FROM tether.alembic_version").fetchall() == [
            ("0005",)
        ]


def test_w5_1_the_app_role_updates_verification_columns_but_cannot_delete(
    seeded: Any, rows: Any
) -> None:
    entry = rows.child("ledger_entries", WS, seeded.run_a, state="indeterminate")
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        rows.insert(conn, "ledger_entries", entry)
    with psycopg.connect(seeded.db.app_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE tether.ledger_entries SET verification_attempts = 1, state = 'completed',"
            " resolution = 'verified_applied', resolved_by = 'system:verification',"
            " verification_evidence_masked = %s WHERE id = %s",
            (Jsonb({"found": True}), entry["id"]),
        )
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("DELETE FROM tether.ledger_entries WHERE id = %s", (entry["id"],))
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        found = conn.execute(
            "SELECT verification_attempts, resolution FROM tether.ledger_entries WHERE id = %s",
            (entry["id"],),
        ).fetchall()
    assert found == [(1, "verified_applied")]


def test_w5_1_a_verified_resolution_is_accepted_with_resolver_and_evidence(
    seeded: Any, rows: Any
) -> None:
    for index, resolution in enumerate(("verified_applied", "verified_not_applied")):
        entry = rows.child(
            "ledger_entries",
            WS,
            seeded.run_a,
            step_ordinal=index,
            idempotency_key="tth_" + "ab"[index] * 52,
            id=uuid.uuid4(),
            resolution=resolution,
            resolved_by="system:verification",
            verification_evidence_masked=Jsonb({"found": index == 0}),
        )
        with psycopg.connect(seeded.db.migrator_dsn) as conn:
            rows.insert(conn, "ledger_entries", entry)
