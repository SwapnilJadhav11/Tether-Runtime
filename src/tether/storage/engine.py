"""Database connectivity (PD-2, PD-3).

- ``create_pool``: the plain psycopg pool used for readiness checks.
- ``create_database_engine``: the async SQLAlchemy engine (psycopg 3 driver) behind the Unit
  of Work.
"""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from sqlalchemy import Engine, create_engine, pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_migration_engine(dsn: str) -> Engine:
    """Sync engine for Alembic (migrations and autogenerate).

    The database roles default to ``search_path = tether``. Alembic treats the default schema
    as unnamed, so with that setting it would see the ``tether`` tables as schema-less and
    autogenerate would misreport them. Migration connections therefore use ``public`` as the
    default schema; all migration DDL is schema-qualified.
    """
    url = make_url(dsn).set(drivername="postgresql+psycopg")
    return create_engine(
        url,
        poolclass=pool.NullPool,
        connect_args={"options": "-c search_path=public"},
    )


def create_database_engine(dsn: str) -> AsyncEngine:
    """Async SQLAlchemy engine for a ``postgresql://`` DSN, using the psycopg 3 driver."""
    url = make_url(dsn).set(drivername="postgresql+psycopg")
    return create_async_engine(url, pool_pre_ping=True)


def create_pool(
    dsn: str, *, name: str, min_size: int = 1, max_size: int = 10
) -> AsyncConnectionPool:
    """Create an unopened pool; the caller opens it with ``await pool.open(wait=False)``."""
    return AsyncConnectionPool(
        conninfo=dsn, name=name, min_size=min_size, max_size=max_size, open=False
    )


async def database_is_reachable(pool: AsyncConnectionPool, *, timeout: float) -> bool:
    """Return whether a pooled connection can run a trivial query within ``timeout`` seconds."""
    try:
        async with pool.connection(timeout=timeout) as conn:
            await conn.execute("SELECT 1")
    except (PoolTimeout, psycopg.Error):
        return False
    return True
