"""Shared test configuration, including the test compose Postgres connection details."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from urllib.parse import quote

import pytest

# psycopg's async driver cannot run on Windows' default ProactorEventLoop. Production runs on
# Linux; this only affects tests on the Windows development host.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REQUIRED_ENV = ("TETHER_MIGRATOR_DB_PASSWORD", "TETHER_APP_DB_PASSWORD")


@dataclass(frozen=True)
class TestDatabase:
    """The Postgres from deploy/compose/docker-compose.test.yml."""

    __test__ = False  # not a pytest test class

    host: str
    port: int
    migrator_password: str
    app_password: str

    def _dsn(self, role: str, password: str) -> str:
        return f"postgresql://{role}:{quote(password, safe='')}@{self.host}:{self.port}/tether"

    @property
    def migrator_dsn(self) -> str:
        return self._dsn("tether_migrator", self.migrator_password)

    @property
    def app_dsn(self) -> str:
        return self._dsn("tether_app", self.app_password)


@pytest.fixture(scope="session")
def test_db() -> TestDatabase:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.fail(
            f"missing {', '.join(missing)}: run `uv run poe test-integration` or "
            "`uv run poe test-contract`, which start deploy/compose/docker-compose.test.yml and "
            "load deploy/compose/.env (copy deploy/compose/.env.example)",
            pytrace=False,
        )
    return TestDatabase(
        host=os.environ.get("TETHER_TEST_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TETHER_TEST_PG_PORT", "55432")),
        migrator_password=os.environ["TETHER_MIGRATOR_DB_PASSWORD"],
        app_password=os.environ["TETHER_APP_DB_PASSWORD"],
    )
