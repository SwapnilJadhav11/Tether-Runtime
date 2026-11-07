"""S1.5 (moved from S1.3 contract (f)): autogenerate matches the schema and ignores LangGraph.

Uses the real ``target_metadata`` and the same context options as the migration environment.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from tether.storage.engine import create_migration_engine
from tether.storage.tables import MIGRATION_CONTEXT_OPTIONS, metadata

SAVER_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}


def _diff(dsn: str, options: dict[str, Any]) -> list[Any]:
    engine = create_migration_engine(dsn)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={**options, "target_metadata": metadata}
            )
            return list(compare_metadata(context, metadata))
    finally:
        engine.dispose()


def test_autogenerate_reports_no_differences(fresh_db: Any) -> None:
    """The migration matches target_metadata; LangGraph's tables are not proposed for removal."""
    assert _diff(fresh_db.migrator_dsn, MIGRATION_CONTEXT_OPTIONS) == []


def test_langgraph_tables_exist_in_the_compared_database(fresh_db: Any) -> None:
    with psycopg.connect(fresh_db.migrator_dsn) as conn:
        found = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'langgraph'"
        ).fetchall()
    assert {row[0] for row in found} == SAVER_TABLES


def test_without_the_schema_filter_langgraph_tables_would_be_flagged(fresh_db: Any) -> None:
    """Control: the filter is what keeps the saver's tables out of autogenerate."""
    unfiltered = {
        key: value for key, value in MIGRATION_CONTEXT_OPTIONS.items() if key != "include_name"
    }
    removed = {
        (diff[1].schema, diff[1].name)
        for diff in _diff(fresh_db.migrator_dsn, unfiltered)
        if isinstance(diff, tuple) and diff[0] == "remove_table"
    }
    assert {("langgraph", name) for name in SAVER_TABLES} <= removed


@pytest.mark.parametrize("schema", ["langgraph", "public"])
def test_only_the_tether_schema_is_included(schema: str) -> None:
    include_name = MIGRATION_CONTEXT_OPTIONS["include_name"]
    assert include_name(schema, "schema", {}) is False
    assert include_name("tether", "schema", {}) is True
