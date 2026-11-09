"""Regression (found by the S5.1 stop-vs-TX5 chaos race): TX7 must follow the global lock order.

Architecture §13.1 fixes the lock order: run row → approval/ledger row → audit head → jobs. TX7
(the outcome of a send) used to skip the run row: it locked the audit head, then its inserts'
foreign-key checks needed a share lock on the run row. A concurrent TX9/TX10/TX-EA holds the run
row ``FOR UPDATE`` and then locks the audit head, so the two deadlocked and Postgres aborted one:
a cancel failed, or a completed write's outcome was rolled back and later became indeterminate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from services.demo_target_service.app import create_app as create_demo_target
from tests.chaos.harness import TARGET, make_env, reset_target, wait_until
from workloads.incident_remediation import client as demo_client


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    yield
    demo_client.set_transport(None)
    reset_target()


def _blocked_on_a_lock(admin: psycopg.Connection[Any], dbname: str, other_than: int) -> bool:
    rows = admin.execute(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE datname = %s AND wait_event_type = 'Lock' AND pid <> %s",
        (dbname, other_than),
    ).fetchone()
    return bool(rows and rows[0])


@pytest.mark.asyncio
async def test_tx7_takes_the_run_row_first_so_a_concurrent_stop_cannot_deadlock_it(
    seeded: Any, tmp_path: Path
) -> None:
    env = make_env(seeded.db, tmp_path)
    gate = asyncio.Event()
    reset_target(gate)
    worker = await env.worker()
    try:
        run_id = await env.submit("write")
        sending = asyncio.create_task(env.drain(worker))
        await wait_until(lambda: TARGET.in_flight == 1)  # TX6 committed, the write is in flight

        with (
            psycopg.connect(seeded.db.app_dsn) as stop,
            psycopg.connect(seeded.db.superuser_dsn(seeded.db.dbname), autocommit=True) as admin,
        ):
            stop_pid = stop.info.backend_pid
            # A stop transaction (TX9/TX10/TX-EA) takes the run row first ...
            stop.execute("SELECT id FROM tether.runs WHERE id = %s FOR UPDATE", (run_id,))
            gate.set()  # ... while the write returns and TX7 starts
            await wait_until(lambda: _blocked_on_a_lock(admin, seeded.db.dbname, stop_pid))
            # ... and then the audit head, which TX7 must not be holding.
            stop.execute("SET LOCAL lock_timeout = '5s'")
            stop.execute(
                "SELECT run_id FROM tether.audit_heads WHERE run_id = %s FOR UPDATE", (run_id,)
            )
            stop.commit()

        await sending  # TX7 then completes: no deadlock, nothing rolled back
        [(state,)] = env.sql("SELECT state FROM tether.ledger_entries WHERE run_id = %s", run_id)
        assert state == "completed"
        assert env.events(run_id).count("TOOL_OUTCOME") == 1
        await env.drain(worker)
        assert env.status(run_id) == "succeeded"
        await env.assert_reconstructable(run_id)
    finally:
        await worker.close()
