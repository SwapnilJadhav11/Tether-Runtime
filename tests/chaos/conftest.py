"""Chaos tests reuse the integration database fixtures: a migrated template cloned per test."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from services.demo_target_service.app import create_app as create_demo_target
from tests.chaos.harness import Env, make_env, reset_target
from tests.integration.conftest import fresh_db, rows, seeded, template_database
from workloads.incident_remediation import client as demo_client

from tether.queue import backoff

__all__ = ["fresh_db", "rows", "seeded", "template_database"]


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[Any]:
    """An in-process demo target per test; its ``restarts`` counter is the side-effect count."""
    app = create_demo_target()
    demo_client.set_transport(httpx.ASGITransport(app=app))
    reset_target()
    yield app
    demo_client.set_transport(None)


@pytest_asyncio.fixture
async def env(seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    # Retries are due at once (PD-13 backoff is covered by the retry tests), so a drain reaches
    # every outcome without sleeping.
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
    yield make_env(seeded.db, tmp_path)
