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
