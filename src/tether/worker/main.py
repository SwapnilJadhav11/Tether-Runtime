"""Worker process entrypoint.

Each poll interval the worker checks database connectivity and, while the database is reachable,
refreshes a heartbeat file that the container healthcheck reads and processes every due job.
It leases ``advance_run``, ``execute_tool`` and the two timers (``run_deadline``,
``expire_approval``). A job leased past its ``max_attempts`` goes to its TX12 dead-letter
transition instead of its handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from tether.core.statuses import JobKind
from tether.queue import postgres as queue
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.storage.engine import create_pool, database_is_reachable
from tether.worker.handlers import advance_run, execute_tool, timers

logger = logging.getLogger(__name__)

# A job whose run lock is held elsewhere is retried after this delay, without using an attempt.
CONTENDED_RETRY_SECONDS = 1.0


def heartbeat_is_fresh(path: Path, *, max_age_seconds: float) -> bool:
    try:
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - modified <= max_age_seconds


TIMER_KINDS = {
    JobKind.RUN_DEADLINE: timers.run_deadline,
    JobKind.EXPIRE_APPROVAL: timers.expire_approval,
}
HANDLED_KINDS = (JobKind.ADVANCE_RUN, JobKind.EXECUTE_TOOL, *TIMER_KINDS)


async def process_next_job(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> bool:
    """Lease and process one due job of ``kinds`` (default: all handled); ``False`` if none."""
    engine = runtime.engine
    job = await queue.lease_one(
        engine, kinds=kinds or HANDLED_KINDS, lease_seconds=runtime.settings.worker_lease_seconds
    )
    if job is None:
        return False
    if job.attempts > job.max_attempts:
        # Bounded delivery (§11 AD-08): TX12 instead of the handler; it marks the job dead.
        if job.kind == JobKind.EXECUTE_TOOL:
            await execute_tool.dead_letter(runtime, job)
        elif job.kind in TIMER_KINDS:
            await timers.dead_letter(runtime, job)
        elif await advance_run.dead_letter(runtime, job) is advance_run.Outcome.CONTENDED:
            # Another driver holds the run: try TX12 again later, without spending an attempt.
            await queue.release(
                engine, job, delay_seconds=CONTENDED_RETRY_SECONDS, refund_attempt=True
            )
        return True
    if job.kind == JobKind.EXECUTE_TOOL:
        retry_in = await execute_tool.handle(runtime, job)
        if retry_in is None:
            # TX7 is already committed; completing needs the current lease token (no rollback).
            await queue.complete(engine, job)
        else:
            # A retry is this job delivered again after its backoff; the attempt stays spent.
            await queue.release(engine, job, delay_seconds=retry_in, refund_attempt=False)
        return True
    if job.kind in TIMER_KINDS:
        await TIMER_KINDS[JobKind(job.kind)](runtime, job)
        await queue.complete(engine, job)
        return True
    outcome = await advance_run.handle(runtime, job)
    if outcome is advance_run.Outcome.COMPLETED:
        await queue.complete(engine, job)
    elif outcome in (advance_run.Outcome.CONTENDED, advance_run.Outcome.STALE):
        await queue.release(engine, job, delay_seconds=CONTENDED_RETRY_SECONDS, refund_attempt=True)
    else:
        # Fenced: leave the run to the newer driver; this job is retried and re-checks the run.
        await queue.release(engine, job, delay_seconds=0, refund_attempt=False)
    return True


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    dsn = settings.database_url.get_secret_value()
    pool = create_pool(dsn, name="tether-worker")
    runtime = await WorkerRuntime.open(settings)
    await pool.open(wait=False)
    try:
        while not stop.is_set():
            if await database_is_reachable(pool, timeout=settings.readiness_timeout_seconds):
                settings.worker_heartbeat_file.touch()
                try:
                    while not stop.is_set() and await process_next_job(runtime):
                        pass
                except Exception:
                    # The job keeps its lease and is retried after it expires; keep serving.
                    logger.exception("job processing failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval_seconds)
    finally:
        await runtime.close()
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
