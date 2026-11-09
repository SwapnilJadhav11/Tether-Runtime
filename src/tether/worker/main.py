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

from opentelemetry.trace import SpanKind

from tether.core.statuses import JobKind
from tether.observability import metrics, tracing
from tether.observability.logging import bound, get_logger
from tether.observability.telemetry import setup_telemetry
from tether.queue import postgres as queue
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.storage.engine import create_pool, database_is_reachable
from tether.worker.handlers import advance_run, execute_tool, timers

logger = logging.getLogger(__name__)
_log = get_logger(__name__)
_LOGGED = "_tether_logged"

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
    started = time.monotonic()
    outcome = "error"
    # One span per delivery: a child of the run's root context, linked to the span that
    # enqueued the job (AD-13), so a run is one trace across processes and restarts.
    with (
        bound(job_id=job.id, job_kind=job.kind, run_id=job.run_id, workspace_id=job.workspace_id),
        tracing.span(
            f"job {job.kind}",
            parent=job.run_trace_parent or job.trace_parent,
            links=[job.trace_parent],
            kind=SpanKind.CONSUMER,
            attributes={
                "tether.job.id": job.id,
                "tether.job.kind": job.kind,
                "tether.job.attempt": job.attempts,
                "tether.job.max_attempts": job.max_attempts,
                "tether.job.waited_seconds": round(job.waited_seconds, 3),
                "tether.run_id": job.run_id,
                "tether.workspace_id": job.workspace_id,
            },
        ) as span,
    ):
        metrics.job_picked_up(job.kind, job.waited_seconds)
        try:
            outcome = await _dispatch(runtime, job)
        except Exception as error:
            _log.exception("job processing failed", attempt=job.attempts)
            # Marked so the worker loop does not log it a second time.
            setattr(error, _LOGGED, True)
            raise
        finally:
            elapsed = time.monotonic() - started
            metrics.job_processed(job.kind, outcome, elapsed)
            tracing.annotate(span, **{"tether.job.outcome": outcome})
        _log.info(
            "job processed",
            outcome=outcome,
            attempt=job.attempts,
            duration_ms=round(elapsed * 1000, 1),
        )
    return True


async def _dispatch(runtime: WorkerRuntime, job: queue.LeasedJob) -> str:
    """Handle one leased job; returns a bounded outcome label for telemetry."""
    engine = runtime.engine
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
            return "contended"
        return "dead_letter"
    if job.kind == JobKind.EXECUTE_TOOL:
        retry_in = await execute_tool.handle(runtime, job)
        if retry_in is None:
            # TX7 is already committed; completing needs the current lease token (no rollback).
            await queue.complete(engine, job)
            return "completed"
        # A retry is this job delivered again after its backoff; the attempt stays spent.
        await queue.release(engine, job, delay_seconds=retry_in, refund_attempt=False)
        return "retry"
    if job.kind in TIMER_KINDS:
        await TIMER_KINDS[JobKind(job.kind)](runtime, job)
        await queue.complete(engine, job)
        return "completed"
    outcome = await advance_run.handle(runtime, job)
    if outcome is advance_run.Outcome.COMPLETED:
        await queue.complete(engine, job)
    elif outcome in (advance_run.Outcome.CONTENDED, advance_run.Outcome.STALE):
        await queue.release(engine, job, delay_seconds=CONTENDED_RETRY_SECONDS, refund_attempt=True)
    else:
        # Fenced: leave the run to the newer driver; this job is retried and re-checks the run.
        await queue.release(engine, job, delay_seconds=0, refund_attempt=False)
    return str(outcome.value)


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    dsn = settings.database_url.get_secret_value()
    pool = create_pool(dsn, name="tether-worker")
    runtime = await WorkerRuntime.open(settings)
    await pool.open(wait=False)
    next_depth_sample = 0.0
    try:
        while not stop.is_set():
            if await database_is_reachable(pool, timeout=settings.readiness_timeout_seconds):
                settings.worker_heartbeat_file.touch()
                if time.monotonic() >= next_depth_sample:
                    next_depth_sample = time.monotonic() + settings.otel_metric_interval_seconds
                    await _sample_queue_depth(runtime)
                try:
                    while not stop.is_set() and await process_next_job(runtime):
                        pass
                except Exception as error:
                    # The job keeps its lease and is retried after it expires; keep serving.
                    # A failure inside a job was already logged with its job context.
                    if not getattr(error, _LOGGED, False):
                        logger.exception("job processing failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval_seconds)
    finally:
        await runtime.close()
        await pool.close()


async def _sample_queue_depth(runtime: WorkerRuntime) -> None:
    """Refresh the queue-depth gauge; telemetry only, so a failure is ignored."""
    try:
        metrics.set_queue_depth(await queue.depth(runtime.engine))
    except Exception:
        logger.debug("queue depth sample failed", exc_info=True)


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
    settings = Settings()
    telemetry = setup_telemetry(settings, service="tether-worker")
    try:
        asyncio.run(_serve(settings))
    finally:
        telemetry.shutdown()


def healthcheck() -> None:
    """Container healthcheck: exit 0 while the worker heartbeat is fresh."""
    settings = Settings()
    fresh = heartbeat_is_fresh(
        settings.worker_heartbeat_file, max_age_seconds=settings.worker_heartbeat_max_age_seconds
    )
    sys.exit(0 if fresh else 1)
