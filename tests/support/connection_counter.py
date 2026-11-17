"""Opt-in pytest plugin that counts the database connections a test session opens.

Enable with ``-p tests.support.connection_counter``. Every ``psycopg`` connect (sync and async,
which covers pool members and SQLAlchemy engines) is counted by target database kind, and a
summary is printed at the end of the session. It changes no behaviour: the original connect is
always called. Connections made by child processes are not seen.
"""

from __future__ import annotations

import collections
from typing import Any

import psycopg
import pytest

COUNTS: collections.Counter[str] = collections.Counter()


def _kind(conninfo: str, kwargs: dict[str, Any]) -> str:
    text = conninfo or ""
    dbname = kwargs.get("dbname") or ""
    if "/" in text:
        dbname = dbname or text.rsplit("/", 1)[-1].split("?", 1)[0]
    if "postgres://" in text or "postgresql://" in text:
        user = text.split("://", 1)[1].split(":", 1)[0]
    else:
        user = str(kwargs.get("user") or "")
    if user == "postgres":
        return "admin(superuser)"
    return "test-database" if dbname.startswith("tether") else "other"


def _wrap_sync(original: Any) -> Any:
    def connect(cls: Any, conninfo: str = "", *args: Any, **kwargs: Any) -> Any:
        COUNTS[_kind(conninfo, kwargs)] += 1
        return original.__func__(cls, conninfo, *args, **kwargs)

    return classmethod(connect)


def _wrap_async(original: Any) -> Any:
    async def connect(cls: Any, conninfo: str = "", *args: Any, **kwargs: Any) -> Any:
        COUNTS[_kind(conninfo, kwargs)] += 1
        return await original.__func__(cls, conninfo, *args, **kwargs)

    return classmethod(connect)


def pytest_configure(config: pytest.Config) -> None:
    psycopg.Connection.connect = _wrap_sync(psycopg.Connection.__dict__["connect"])  # type: ignore[method-assign,assignment]
    # ``psycopg.connect`` was bound to the original classmethod at import time: rebind it.
    psycopg.connect = psycopg.Connection.connect
    psycopg.AsyncConnection.connect = _wrap_async(  # type: ignore[method-assign,assignment]
        psycopg.AsyncConnection.__dict__["connect"]
    )


def pytest_terminal_summary(terminalreporter: Any) -> None:
    total = sum(COUNTS.values())
    terminalreporter.write_sep("-", "database connections opened")
    for kind, count in sorted(COUNTS.items()):
        terminalreporter.write_line(f"  {kind}: {count}")
    terminalreporter.write_line(f"  TOTAL: {total}")
