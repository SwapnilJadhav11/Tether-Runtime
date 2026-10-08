"""S1.2: the compose Postgres, migrations and readiness work together (ADR-0001, PD-2, PD-4).

Runs against ``deploy/compose/docker-compose.test.yml``; start it with
``uv run poe test-integration``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import errors
from pydantic import SecretStr

from tether.api.app import create_app
from tether.settings import Settings

if TYPE_CHECKING:
    from conftest import TestDatabase

BASELINE_REVISION = "0001"


def _run_migrate(test_db: TestDatabase) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TETHER_MIGRATOR_DATABASE_URL": test_db.migrator_dsn}
    return subprocess.run(
        [sys.executable, "-m", "tether.cli.main", "migrate"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _versions(test_db: TestDatabase) -> list[str]:
    with psycopg.connect(test_db.migrator_dsn) as conn:
        rows = conn.execute("SELECT version_num FROM tether.alembic_version").fetchall()
    return [row[0] for row in rows]


def test_migrate_exits_zero_and_is_idempotent(test_db: TestDatabase) -> None:
    first = _run_migrate(test_db)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _versions(test_db) == [BASELINE_REVISION]

    second = _run_migrate(test_db)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _versions(test_db) == [BASELINE_REVISION]


SAVER_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)


def _saver_table_schemas(test_db: TestDatabase) -> set[tuple[str, str]]:
    with psycopg.connect(test_db.migrator_dsn) as conn:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables"
            " WHERE table_name = ANY(%s)",
            (sorted(SAVER_TABLES),),
        ).fetchall()
    return {(row[0], row[1]) for row in rows}


def test_migrate_creates_saver_tables_only_in_langgraph_schema(test_db: TestDatabase) -> None:
    result = _run_migrate(test_db)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _saver_table_schemas(test_db) == {("langgraph", name) for name in SAVER_TABLES}


def test_saver_setup_is_idempotent(test_db: TestDatabase) -> None:
    def applied() -> list[int]:
        with psycopg.connect(test_db.migrator_dsn) as conn:
            rows = conn.execute("SELECT v FROM langgraph.checkpoint_migrations ORDER BY v")
            return [row[0] for row in rows.fetchall()]

    assert _run_migrate(test_db).returncode == 0
    before = applied()
    assert _run_migrate(test_db).returncode == 0
    assert applied() == before
    assert before, "the saver recorded no migrations"


@pytest.mark.parametrize("table", sorted(SAVER_TABLES - {"checkpoint_migrations"}))
def test_app_role_can_read_and_write_saver_tables(test_db: TestDatabase, table: str) -> None:
    assert _run_migrate(test_db).returncode == 0
    with psycopg.connect(test_db.migrator_dsn) as conn:
        row = conn.execute(
            "SELECT has_table_privilege('tether_app', %s, 'SELECT, INSERT, UPDATE, DELETE')",
            (f"langgraph.{table}",),
        ).fetchone()
    assert row is not None
    assert row[0] is True


def test_tether_and_langgraph_schemas_exist(test_db: TestDatabase) -> None:
    with psycopg.connect(test_db.app_dsn) as conn:
        rows = conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname IN ('tether', 'langgraph')"
        ).fetchall()
    assert {row[0] for row in rows} == {"tether", "langgraph"}


def test_readyz_returns_200_against_the_database(test_db: TestDatabase) -> None:
    settings = Settings(database_url=SecretStr(test_db.app_dsn))
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


DDL_STATEMENTS = (
    "CREATE TABLE tether.ddl_probe (id int)",
    "CREATE TABLE langgraph.ddl_probe (id int)",
    "CREATE TABLE public.ddl_probe (id int)",
    "CREATE SCHEMA ddl_probe",
    "CREATE TEMPORARY TABLE ddl_probe (id int)",
)


@pytest.mark.parametrize("statement", DDL_STATEMENTS)
def test_app_role_cannot_run_ddl(test_db: TestDatabase, statement: str) -> None:
    with psycopg.connect(test_db.app_dsn) as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement)


def test_migrator_role_can_run_ddl_in_tether_schema(test_db: TestDatabase) -> None:
    """Control for the DDL test above: the same statement succeeds for the migrator role."""
    with psycopg.connect(test_db.migrator_dsn) as conn:
        conn.execute("CREATE TABLE tether.ddl_probe (id int)")
        conn.rollback()
