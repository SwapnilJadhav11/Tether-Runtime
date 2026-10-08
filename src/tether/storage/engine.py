"""Database connectivity: the async psycopg connection pool (PD-2)."""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout


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
