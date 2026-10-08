"""Worker process entrypoint.

S1.2 runs an idle loop: it checks database connectivity each interval and, while the database
is reachable, refreshes a heartbeat file that the container healthcheck reads. Job leasing
arrives with the job queue (S2.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time
from pathlib import Path

from tether.settings import Settings
from tether.storage.engine import create_pool, database_is_reachable


def heartbeat_is_fresh(path: Path, *, max_age_seconds: float) -> bool:
    try:
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - modified <= max_age_seconds


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    pool = create_pool(settings.database_url.get_secret_value(), name="tether-worker")
    await pool.open(wait=False)
    try:
        while not stop.is_set():
            if await database_is_reachable(pool, timeout=settings.readiness_timeout_seconds):
                settings.worker_heartbeat_file.touch()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval_seconds)
    finally:
        await pool.close()


async def _serve(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Signal handlers on the loop are unavailable on Windows; Ctrl+C still interrupts there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await run_worker(settings, stop)


def main() -> None:
    """Entrypoint for the ``worker`` process."""
    asyncio.run(_serve(Settings()))


def healthcheck() -> None:
    """Container healthcheck: exit 0 while the worker heartbeat is fresh."""
    settings = Settings()
    fresh = heartbeat_is_fresh(
        settings.worker_heartbeat_file, max_age_seconds=settings.worker_heartbeat_max_age_seconds
    )
    sys.exit(0 if fresh else 1)
