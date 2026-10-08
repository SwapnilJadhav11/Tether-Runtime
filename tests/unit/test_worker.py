from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from pydantic import SecretStr

from tether.settings import Settings
from tether.worker.main import heartbeat_is_fresh, run_worker

UNREACHABLE_DSN = SecretStr("postgresql://tether_app:x@127.0.0.1:1/tether")


def test_missing_heartbeat_is_not_fresh(tmp_path: Path) -> None:
    assert not heartbeat_is_fresh(tmp_path / "missing", max_age_seconds=30)


def test_recent_heartbeat_is_fresh(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    beat.touch()
    assert heartbeat_is_fresh(beat, max_age_seconds=30)


def test_old_heartbeat_is_not_fresh(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    beat.touch()
    old = time.time() - 120
    os.utime(beat, (old, old))
    assert not heartbeat_is_fresh(beat, max_age_seconds=30)


def test_worker_stops_on_request_and_writes_no_heartbeat_without_a_database(
    tmp_path: Path,
) -> None:
    beat = tmp_path / "beat"
    settings = Settings(
        database_url=UNREACHABLE_DSN,
        readiness_timeout_seconds=0.2,
        worker_poll_interval_seconds=0.05,
        worker_heartbeat_file=beat,
    )

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_worker(settings, stop))
        await asyncio.sleep(0.5)
        stop.set()
        # Closing the pool waits up to psycopg's default 5 s for an in-flight reconnect attempt,
        # so allow more than that before declaring the worker stuck.
        await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    assert not beat.exists()
