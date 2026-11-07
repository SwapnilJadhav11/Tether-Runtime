"""Template-database fixtures and row builders for S1.5 integration tests (PD-8).

Each test that needs isolated data gets its own database cloned from a migrated template with
``CREATE DATABASE ... TEMPLATE``. Cloning copies schemas, tables, object grants, triggers and
default privileges, but not the database ACL or per-database role settings, so those are
re-applied to every clone exactly as deploy/compose/postgres/init.sh sets them on ``tether``.
Clones are created and dropped with the test-only Postgres superuser.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.types.json import Jsonb

from tether.cli.main import MIGRATIONS_LOCATION, upgrade_to_head
from tether.runtime.checkpointer import setup_langgraph_schema

if TYPE_CHECKING:
    from conftest import TestDatabase

SOURCE_DB = "tether"
TEMPLATE_DB = "tether_template"
WS_A = "ws-a"
WS_B = "ws-b"


def apply_database_settings(admin: psycopg.Connection[Any], dbname: str) -> None:
    """Re-apply init.sh's database-level ACL and role settings to a cloned database."""
    database = sql.Identifier(dbname)
    admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
    admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO tether_app").format(database))
    for role in ("tether_migrator", "tether_app"):
        admin.execute(
            sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path = tether").format(
                sql.Identifier(role), database
            )
        )


def _clone(admin: psycopg.Connection[Any], source: str, target: str) -> None:
    admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
        " WHERE datname = %s AND pid <> pg_backend_pid()",
        (source,),
    )
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(target)))
    admin.execute(
        sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER tether_migrator").format(
            sql.Identifier(target), sql.Identifier(source)
        )
    )
    apply_database_settings(admin, target)


def _drop(admin: psycopg.Connection[Any], dbname: str) -> None:
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))


def _rebuild_schema(dsn: str) -> None:
    """Migrate down to the empty baseline and back up, so the current migration code runs.

    The template is cloned from ``tether`` (for init.sh's schemas and default privileges),
    which other tests may already have migrated with older code.
    """
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    config.attributes["database_url"] = dsn
    command.downgrade(config, "base")
    upgrade_to_head(dsn)


@pytest.fixture(scope="session")
def template_database(test_db: TestDatabase) -> Iterator[str]:
    """A fully migrated database (tether schema + LangGraph saver) that tests clone."""
    with psycopg.connect(test_db.superuser_dsn(), autocommit=True) as admin:
        _clone(admin, SOURCE_DB, TEMPLATE_DB)
    template = replace(test_db, dbname=TEMPLATE_DB)
    _rebuild_schema(template.migrator_dsn)
    setup_langgraph_schema(template.migrator_dsn)
    yield TEMPLATE_DB
    with psycopg.connect(test_db.superuser_dsn(), autocommit=True) as admin:
        _drop(admin, TEMPLATE_DB)


@pytest.fixture
def fresh_db(test_db: TestDatabase, template_database: str) -> Iterator[TestDatabase]:
    """A private, migrated database for one test, dropped afterwards."""
    name = f"tether_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(test_db.superuser_dsn(), autocommit=True) as admin:
        _clone(admin, template_database, name)
    try:
        yield replace(test_db, dbname=name)
    finally:
        with psycopg.connect(test_db.superuser_dsn(), autocommit=True) as admin:
            _drop(admin, name)


class Rows:
    """Builders for minimal valid rows. Values are synthetic."""

    def insert(self, conn: psycopg.Connection[Any], table: str, values: Mapping[str, Any]) -> None:
        columns = list(values)
        conn.execute(
            sql.SQL("INSERT INTO tether.{} ({}) VALUES ({})").format(
                sql.Identifier(table),
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() * len(columns)),
            ),
            list(values.values()),
        )

    def run(
        self,
        workspace: str,
        *,
        run_id: uuid.UUID | None = None,
        agent: str = "agent-1",
        status: str = "queued",
    ) -> dict[str, Any]:
        return {
            "id": run_id or uuid.uuid4(),
            "workspace_id": workspace,
            "agent_id": agent,
            "status": status,
            "requester_snapshot": Jsonb({"sub": "user-1", "roles": []}),
            "task_input": Jsonb({"message": "probe"}),
            "deadline_at": datetime.now(UTC) + timedelta(hours=1),
        }

    def child(
        self, table: str, workspace: str, run_id: uuid.UUID, **overrides: Any
    ) -> dict[str, Any]:
        later = datetime.now(UTC) + timedelta(hours=1)
        defaults: dict[str, dict[str, Any]] = {
            "approvals": {
                "id": uuid.uuid4(),
                "step_ordinal": 0,
                "call_hash": "a" * 64,
                "tool": "probe",
                "tool_version": "1.0.0",
                "arguments_masked": Jsonb({}),
                "preconditions_masked": Jsonb({}),
                "status": "pending",
                "expires_at": later,
            },
            "ledger_entries": {
                "id": uuid.uuid4(),
                "step_ordinal": 0,
                "tool": "probe",
                "tool_version": "1.0.0",
                "effect": "write",
                "idempotency_support": "idempotent",
                "call_hash": "a" * 64,
                "idempotency_key": "tth_" + "a" * 52,
                "state": "pending",
            },
            "audit_heads": {"seq": 0, "key_id": "K1"},
            "audit_events": {
                "seq": 1,
                "dedupe_key": "dedupe-1",
                "event_type": "PROBE",
                "payload_masked": Jsonb({}),
                "mac": "b" * 64,
                "key_id": "K1",
            },
            "jobs": {
                "id": uuid.uuid4(),
                "kind": "advance_run",
                "payload": Jsonb({}),
                "max_attempts": 8,
            },
            "model_calls": {
                "id": uuid.uuid4(),
                "turn_ordinal": 0,
                "provider_role": "primary",
                "provider": "scripted",
                "model": "probe-model",
                "attempt": 1,
                "occurrence_id": uuid.uuid4(),
                "status": "succeeded",
                "started_at": datetime.now(UTC),
            },
        }
        return {"workspace_id": workspace, "run_id": run_id, **defaults[table], **overrides}


@pytest.fixture
def rows() -> Rows:
    return Rows()


@dataclass(frozen=True)
class Seeded:
    db: TestDatabase
    run_a: uuid.UUID
    run_b: uuid.UUID


@pytest.fixture
def seeded(fresh_db: TestDatabase, rows: Rows) -> Seeded:
    """Two workspaces, each with ``agent-1`` and one queued run; ``only-a`` exists in ws-a only."""
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(fresh_db.migrator_dsn) as owner:
        for workspace in (WS_A, WS_B):
            rows.insert(owner, "workspaces", {"id": workspace})
            rows.insert(owner, "agents", {"workspace_id": workspace, "agent_id": "agent-1"})
        rows.insert(owner, "agents", {"workspace_id": WS_A, "agent_id": "only-a"})
        rows.insert(owner, "runs", rows.run(WS_A, run_id=run_a))
        rows.insert(owner, "runs", rows.run(WS_B, run_id=run_b))
    return Seeded(fresh_db, run_a, run_b)
