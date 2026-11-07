"""S1.5: what the least-privilege application role may do (ADR-0015, ADR-0014, owner decisions)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import errors, sql

BOOTSTRAP_TABLES = (
    "workspaces",
    "issuers",
    "api_keys",
    "agents",
    "api_key_agents",
    "policy_versions",
)
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

EXPECTED: dict[str, set[str]] = {
    **{table: {"SELECT"} for table in BOOTSTRAP_TABLES},
    "runs": {"SELECT", "INSERT", "UPDATE"},
    "approvals": {"SELECT", "INSERT", "UPDATE"},
    "ledger_entries": {"SELECT", "INSERT", "UPDATE"},
    "model_calls": {"SELECT", "INSERT", "UPDATE"},
    "audit_events": {"SELECT", "INSERT"},
    "access_events": {"SELECT", "INSERT"},
    # Runtime code transitions these rows; it never removes them (owner decision).
    "audit_heads": {"SELECT", "INSERT", "UPDATE"},
    "jobs": {"SELECT", "INSERT", "UPDATE"},
    # F1: migration bookkeeping is read-only for the app role.
    "alembic_version": {"SELECT"},
}


def test_app_role_privilege_matrix(fresh_db: Any) -> None:
    with psycopg.connect(fresh_db.migrator_dsn, autocommit=True) as conn:
        actual = {
            table: {
                privilege
                for privilege in PRIVILEGES
                if conn.execute(
                    "SELECT has_table_privilege('tether_app', %s, %s)",
                    (f"tether.{table}", privilege),
                ).fetchone()
                == (True,)
            }
            for table in EXPECTED
        }
    assert actual == EXPECTED


@pytest.mark.parametrize("table", BOOTSTRAP_TABLES)
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO tether.{t} DEFAULT VALUES",
        "UPDATE tether.{t} SET workspace_id = workspace_id WHERE false",
        "DELETE FROM tether.{t} WHERE false",
    ],
)
def test_app_role_cannot_write_bootstrap_tables(fresh_db: Any, table: str, statement: str) -> None:
    sql_text = statement.format(t=table)
    if table == "workspaces":
        sql_text = sql_text.replace("workspace_id = workspace_id", "id = id")
    with (
        psycopg.connect(fresh_db.app_dsn, autocommit=True) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute(sql_text)


@pytest.mark.parametrize("table", ["audit_events", "access_events"])
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tether.{t} SET workspace_id = workspace_id WHERE false",
        "DELETE FROM tether.{t} WHERE false",
        "TRUNCATE tether.{t}",
    ],
)
def test_audit_and_access_events_are_append_only(fresh_db: Any, table: str, statement: str) -> None:
    with (
        psycopg.connect(fresh_db.app_dsn, autocommit=True) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute(statement.format(t=table))


@pytest.mark.parametrize(
    "table", ["runs", "approvals", "ledger_entries", "model_calls", "audit_heads", "jobs"]
)
def test_app_role_cannot_delete_records(fresh_db: Any, table: str) -> None:
    with (
        psycopg.connect(fresh_db.app_dsn, autocommit=True) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute(sql.SQL("DELETE FROM tether.{} WHERE false").format(sql.Identifier(table)))


def test_app_role_cannot_disable_the_terminal_status_guard(fresh_db: Any) -> None:
    with (
        psycopg.connect(fresh_db.app_dsn, autocommit=True) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute("ALTER TABLE tether.runs DISABLE TRIGGER runs_status_terminal_guard")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tether.alembic_version SET version_num = '0001'",
        "DELETE FROM tether.alembic_version",
        "INSERT INTO tether.alembic_version (version_num) VALUES ('9999')",
        "TRUNCATE tether.alembic_version",
    ],
)
def test_app_role_cannot_modify_alembic_version(fresh_db: Any, statement: str) -> None:
    """F1: a compromised api/worker must not be able to rewrite the migration state."""
    with (
        psycopg.connect(fresh_db.app_dsn, autocommit=True) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute(statement)


@pytest.mark.parametrize("lock", ["FOR SHARE", "FOR KEY SHARE"])
def test_app_role_can_share_lock_policy_versions(fresh_db: Any, lock: str) -> None:
    """F2 / PD-31: policy decisions take policy_versions FOR SHARE as tether_app."""
    with psycopg.connect(fresh_db.app_dsn) as conn:
        conn.execute(sql.SQL("SELECT version FROM tether.policy_versions ") + sql.SQL(lock))
        conn.rollback()


@pytest.mark.parametrize("column", ["workspace_id", "version", "active"])
def test_policy_fields_stay_read_only_for_the_app_role(fresh_db: Any, column: str) -> None:
    """F2: the column grant that enables row locks does not allow changing policy data."""
    with (
        psycopg.connect(fresh_db.app_dsn, autocommit=True) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute(
            sql.SQL("UPDATE tether.policy_versions SET {c} = {c} WHERE false").format(
                c=sql.Identifier(column)
            )
        )
