"""S1.5 exit criterion: the template-database clone fixture works (PD-8)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import psycopg
import pytest
from psycopg import errors


def test_clone_is_fully_migrated(fresh_db: Any) -> None:
    with psycopg.connect(fresh_db.migrator_dsn) as conn:
        assert conn.execute("SELECT version_num FROM tether.alembic_version").fetchall() == [
            ("0002",)
        ]
        saver = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'langgraph'"
        ).fetchone()
    assert saver == (4,)


def test_clone_has_the_app_roles_search_path(fresh_db: Any) -> None:
    with psycopg.connect(fresh_db.app_dsn) as conn:
        assert conn.execute("SHOW search_path").fetchone() == ("tether",)
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (0,)


def test_clone_reapplies_the_database_acl(fresh_db: Any) -> None:
    """Without the re-applied REVOKE, PUBLIC's default TEMP privilege would allow this."""
    with (
        psycopg.connect(fresh_db.app_dsn, autocommit=True) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute("CREATE TEMPORARY TABLE probe (id int)")


def test_clones_are_isolated_from_the_template(fresh_db: Any, template_database: str) -> None:
    with psycopg.connect(fresh_db.migrator_dsn) as conn:
        conn.execute("INSERT INTO tether.workspaces (id) VALUES ('only-in-clone')")
    template = replace(fresh_db, dbname=template_database)
    with psycopg.connect(template.migrator_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM tether.workspaces").fetchone() == (0,)
