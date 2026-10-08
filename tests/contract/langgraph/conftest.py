"""Fixtures for the LangGraph semantics contract suite (S1.3, ADR-0003/0005)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from tether.cli.main import upgrade_to_head
from tether.runtime.checkpointer import (
    create_checkpointer,
    create_saver_pool,
    setup_langgraph_schema,
)

if TYPE_CHECKING:
    from conftest import TestDatabase

CRASH_CHILD = Path(__file__).with_name("crash_child.py")
CRASH_EXIT_CODE = 137


@pytest.fixture(scope="session")
def migrated_db(test_db: TestDatabase) -> TestDatabase:
    """The test database with the tether migrations and the saver tables in place."""
    upgrade_to_head(test_db.migrator_dsn)
    setup_langgraph_schema(test_db.migrator_dsn)
    return test_db


@pytest_asyncio.fixture
async def checkpointer(migrated_db: TestDatabase) -> AsyncIterator[AsyncPostgresSaver]:
    """A saver from the production factory, connected as the application role (as the worker)."""
    pool = create_saver_pool(migrated_db.app_dsn, name="contract-tests")
    await pool.open(wait=True, timeout=10)
    try:
        yield create_checkpointer(pool)
    finally:
        await pool.close()


@pytest.fixture
def thread_config() -> dict[str, Any]:
    return {"configurable": {"thread_id": f"contract-{uuid.uuid4()}"}}


@pytest.fixture(scope="session")
def crash_child() -> ModuleType:
    """The crash child module, so tests build exactly the graph the child process ran."""
    spec = importlib.util.spec_from_file_location("crash_child", CRASH_CHILD)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def crashed_thread(migrated_db: TestDatabase, thread_config: dict[str, Any]) -> dict[str, Any]:
    """A thread whose process was killed with os._exit inside the graph's second node."""
    env = {
        **os.environ,
        "CRASH_CHILD_PATH": str(CRASH_CHILD),
        "CRASH_CHILD_DSN": migrated_db.app_dsn,
        "CRASH_CHILD_THREAD_ID": thread_config["configurable"]["thread_id"],
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, runpy; runpy.run_path(os.environ['CRASH_CHILD_PATH'], run_name='__main__')",
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == CRASH_EXIT_CODE, result.stdout + result.stderr
    return thread_config
