"""W6.1 under chaos: losing a job's lease mid-send is the same as the worker dying.

The lease passes to another lessee while a write is in flight. The heartbeat notices, the handler
is aborted and nothing is recorded by that delivery. The next delivery then applies the ledger's
existing rules: an idempotent write is re-sent with the same key and applies once; a
non-idempotent write becomes ``indeterminate`` and is never re-sent.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from tests.chaos.harness import TARGET, Env, reset_target, wait_until

pytestmark = pytest.mark.chaos

SHORT_LEASE = {"worker_lease_seconds": 1.5, "worker_lease_heartbeat_seconds": 0.3}


def take_over(env: Env, run_id: uuid.UUID) -> None:
    """Another lessee took the job and then died: a foreign token and an expired lease."""
    env.sql(
        "UPDATE tether.jobs SET lease_token = %s, lease_expires_at = clock_timestamp()"
        " - interval '1 second' WHERE run_id = %s AND kind = 'execute_tool' AND state = 'leased'"
        " RETURNING id",
        uuid.uuid4(),
        run_id,
    )


@pytest.mark.parametrize("repeat", range(3))
@pytest.mark.asyncio
async def test_t29_heartbeat_loss_on_a_non_idempotent_write_is_worker_death(
    env: Env, repeat: int
) -> None:
    env.overrides = SHORT_LEASE
    worker = await env.worker()
    try:
        reset_target(asyncio.Event())  # the send never answers: it can only be aborted
        run_id = await env.submit("nonidem")
        running = asyncio.create_task(env.drain(worker))
        await wait_until(lambda: TARGET.in_flight == 1)
        take_over(env, run_id)
        await wait_until(lambda: TARGET.in_flight == 0)  # the heartbeat aborted the send
        await running  # the next delivery finds a dispatched non-idempotent write
        assert TARGET.sends == 1
        assert TARGET.nonidem_effects == 0
        assert env.status(run_id) == "awaiting_reconciliation"
        assert [(s, a) for _, _, s, a, _ in env.ledger(run_id)] == [("indeterminate", 1)]
        [(entry_id,)] = env.sql("SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id)
        assert (await env.reconcile(entry_id, "applied")).status_code == 200
        await env.drain(worker)
        assert env.status(run_id) == "succeeded"
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.parametrize("repeat", range(3))
@pytest.mark.asyncio
async def test_t29_heartbeat_loss_on_an_idempotent_write_re_sends_once_with_the_same_key(
    env: Env, repeat: int
) -> None:
    env.overrides = SHORT_LEASE
    worker = await env.worker()
    try:
        gate = asyncio.Event()
        reset_target(gate)
        run_id = await env.submit("write")
        running = asyncio.create_task(env.drain(worker))
        await wait_until(lambda: TARGET.in_flight == 1)
        take_over(env, run_id)
        await wait_until(lambda: TARGET.in_flight == 0)
        gate.set()  # the re-send is answered
        await running
        assert env.status(run_id) == "succeeded"
        assert TARGET.sends == 2
        assert len(set(TARGET.keys)) == 1
        assert len(TARGET.effects) == 1
        assert env.events(run_id).count("TOOL_OUTCOME") == 1
        assert [(s, a) for _, _, s, a, _ in env.ledger(run_id)] == [("completed", 2)]
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
