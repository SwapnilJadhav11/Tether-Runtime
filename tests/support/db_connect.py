"""Connection helpers for the database fixtures: one shared admin connection, a guarded connect.

The DB-backed suites open very many short-lived connections. On the Windows development host
that can exhaust ephemeral ports (``WinError 10048``, "Address already in use"). The first line
of defence is opening fewer connections (``AdminConnection`` is shared for a whole session). The
second is ``connect_with_retry``: a bounded, logged retry of exactly that OS error inside a
fixture's own connect step. It never wraps a test body and never retries any other error.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg

LOGGER = logging.getLogger(__name__)

ATTEMPTS = 5
BASE_DELAY_SECONDS = 0.5
# Windows reports WSAEADDRINUSE as 10048; Linux and macOS spell the message out.
_ADDRESS_IN_USE = ("10048", "address already in use")


def is_address_in_use(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _ADDRESS_IN_USE)


def connect_with_retry(conninfo: str, **kwargs: Any) -> psycopg.Connection[Any]:
    """``psycopg.connect``, retried only when the OS has no free local port to give."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return psycopg.connect(conninfo, **kwargs)
        except psycopg.OperationalError as error:
            if not is_address_in_use(error) or attempt == ATTEMPTS:
                raise
            delay = BASE_DELAY_SECONDS * 2 ** (attempt - 1)
            LOGGER.warning(
                "no free local port for a test database connection (attempt %d of %d); "
                "retrying in %.1f s",
                attempt,
                ATTEMPTS,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class AdminConnection:
    """One autocommit superuser connection reused for every clone and drop in a session.

    Reconnects when the server closed it (for example after a ``pg_terminate_backend`` sweep).
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.Connection[Any] | None = None

    def get(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed or self._conn.broken:
            self.close()
            self._conn = connect_with_retry(self._dsn, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
