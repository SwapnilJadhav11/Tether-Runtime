"""LangGraph Postgres saver on the ``langgraph`` schema (PD-4, PD-22).

LangGraph owns the ``langgraph`` schema; Tether never writes it directly. The saver's tables
are not schema-qualified, so every saver connection pins ``search_path`` to ``langgraph``
(the database roles default to ``tether``).
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

LANGGRAPH_SCHEMA = "langgraph"

# Connection settings the saver requires (see PostgresSaver.from_conn_string): autocommit
# (its migrations use CREATE INDEX CONCURRENTLY), no prepared statements and dict rows.
SAVER_CONNECTION_KWARGS: dict[str, Any] = {
    "autocommit": True,
    "prepare_threshold": 0,
    "row_factory": dict_row,
    "options": f"-c search_path={LANGGRAPH_SCHEMA}",
}


SaverPool = AsyncConnectionPool[AsyncConnection[DictRow]]


def create_saver_pool(
    dsn: str, *, name: str = "tether-langgraph", min_size: int = 1, max_size: int = 4
) -> SaverPool:
    """Create an unopened connection pool configured for the saver.

    The caller opens it (``await pool.open()``) and closes it. Connect as the application
    role: the saver only reads and writes rows; its tables are created by ``migrate``.
    """
    return AsyncConnectionPool(
        conninfo=dsn,
        name=name,
        min_size=min_size,
        max_size=max_size,
        kwargs=dict(SAVER_CONNECTION_KWARGS),
        connection_class=AsyncConnection[DictRow],
        open=False,
    )


def create_checkpointer(pool: SaverPool) -> AsyncPostgresSaver:
    """The checkpointer every run graph is compiled with (invoked with ``durability="sync"``)."""
    return AsyncPostgresSaver(pool)


def setup_langgraph_schema(dsn: str) -> None:
    """Create or upgrade the saver's tables in the ``langgraph`` schema. Idempotent.

    Runs as the migrator role in the one-shot ``migrate`` process.
    """
    with Connection.connect(dsn, **SAVER_CONNECTION_KWARGS) as conn:
        PostgresSaver(conn).setup()
