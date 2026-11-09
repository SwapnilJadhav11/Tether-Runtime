"""Races under real concurrency (separate worker runtimes and api instances on real Postgres).

Plan S5.1's 50x races (TX4 vs TX9, stop vs TX5), competing workers, stops with a write in flight,
overlapping same-key redelivery (C-07, F7b), the approval-expiry race and stale fencing. Each
iteration ends with the T-12 checks; interleavings are varied with small random delays, and the
asserted invariants must hold whichever order wins.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import sys
import uuid
from collections import Counter
from typing import Any

import psycopg
import pytest
from tests.chaos.harness import (
    TARGET,
    TERMINAL,
    Env,
    GatedProvider,
    reset_target,
    restarts,
    wait_until,
)

from tether.worker.main import process_next_job

pytestmark = pytest.mark.chaos
# Deterministic jitter: a hash sequence, so a failure replays the same schedule.
_JITTER = itertools.count()


async def jitter(max_ms: int = 25) -> None:
    digest = hashlib.sha256(f"tether-chaos:{next(_JITTER)}".encode()).digest()
    await asyncio.sleep(int.from_bytes(digest[:4]) % (max_ms + 1) / 1000)


def report(text: str) -> None:
    sys.stdout.write(f"{text}\n")


async def to_approval(env: Env, worker: Any) -> tuple[uuid.UUID, Any]:
    run_id = await env.submit("remediate")
    await env.drain(worker)
    assert env.status(run_id) == "awaiting_approval"
    return run_id, env.approval_id(run_id)


def stopped_then_acted(env: Env, run_id: uuid.UUID) -> bool:
    """True if a new decision or authorization followed a committed stop."""
    events = env.events(run_id)
    if "CANCEL_REQUESTED" not in events:
        return False
    after = events[events.index("CANCEL_REQUESTED") :]
    return "POLICY_DECISION" in after or "EXECUTION_AUTHORIZED" in after


# --- competing workers ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c07_four_workers_draining_fifty_runs_never_duplicate_work(env: Env) -> None:
    workers = [await env.worker() for _ in range(4)]
    try:
        runs = [await env.submit("write") for _ in range(50)]
        await env.drain(*workers)
        assert {env.status(r) for r in runs} == {"succeeded"}
        assert TARGET.sends == 50  # one send per run: no job was processed twice
        assert len(TARGET.effects) == 50
        assert len(set(TARGET.keys)) == 50
        for run_id in runs:
            assert [(s, a) for _, _, s, a, _ in env.ledger(run_id)] == [("completed", 1)]
            assert env.model_calls(run_id) == 2
            await env.assert_reconstructable(run_id)
        leftover = env.sql("SELECT count(*) FROM tether.jobs WHERE state IN ('leased', 'dead')")
        assert leftover == [(0,)]
    finally:
        for worker in workers:
            await worker.close()


@pytest.mark.asyncio
async def test_three_workers_racing_for_one_run_repeated_50_times(env: Env) -> None:
    workers = [await env.worker() for _ in range(3)]
    try:
        for iteration in range(50):
            run_id = await env.submit("write")
            await env.drain(*workers)
            assert env.status(run_id) == "succeeded", iteration
            assert TARGET.sends == iteration + 1
            assert env.model_calls(run_id) == 2
            await env.assert_reconstructable(run_id)
    finally:
        for worker in workers:
            await worker.close()


# --- TX4 vs TX9 (plan S5.1: 50x) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_s5_1_approve_vs_cancel_race_repeated_50_times(env: Env) -> None:
    worker = await env.worker()
    seen: Counter[str] = Counter()
    try:
        for iteration in range(50):
            run_id, approval_id = await to_approval(env, worker)

            async def approve(approval: Any = approval_id) -> int:
                await jitter()
                return (await env.approve(approval)).status_code

            async def cancel(run: uuid.UUID = run_id) -> int:
                await jitter()
                return (await env.cancel(run)).status_code

            approved, cancelled = await asyncio.gather(approve(), cancel())
            await env.drain(worker)
            [(approval_status,)] = env.sql(
                "SELECT status FROM tether.approvals WHERE id = %s", approval_id
            )
            # Row locks serialize them: either the approval was decided first (then the cancel
            # stops the run before TX5), or the cancel closed it first (then approve is 409).
            assert cancelled == 202, iteration
            assert (approved, approval_status) in {(200, "approved"), (409, "closed")}
            seen[approval_status] += 1
            assert env.status(run_id) == "cancelled"
            assert [t for _, t, _, _, _ in env.ledger(run_id)] == ["get_service_health"]
            assert not stopped_then_acted(env, run_id)
            await env.assert_reconstructable(run_id)
        assert await restarts() == 0  # no cancelled run ever restarted the service
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
    report(f"TX4 vs TX9 outcomes over 50 runs: {dict(seen)}")


# --- stop vs TX5 (plan S5.1: 50x) ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_s5_1_cancel_vs_authorization_race_repeated_50_times(env: Env) -> None:
    worker = await env.worker()
    seen: Counter[str] = Counter()
    try:
        for iteration in range(50):
            before = await restarts()
            run_id, approval_id = await to_approval(env, worker)
            assert (await env.approve(approval_id)).status_code == 200

            async def cancel(run: uuid.UUID = run_id) -> int:
                # Wide enough for every order: before TX5, between TX5 and TX6, after the send.
                await jitter(300)
                return (await env.cancel(run)).status_code

            # The wake job runs TX5 (and maybe TX6 and the send) while the cancel lands.
            _, cancelled = await asyncio.gather(env.drain(worker), cancel())
            await env.drain(worker)
            assert cancelled == 202
            status = env.status(run_id)
            applied = await restarts() - before
            write = {t: s for _, t, s, _, _ in env.ledger(run_id)}.get("restart_service")
            [(flag, last_call)] = env.sql(
                "SELECT r.cancel_requested_at, max(m.started_at) FROM tether.runs r"
                " JOIN tether.model_calls m ON m.run_id = r.id WHERE r.id = %s"
                " GROUP BY r.cancel_requested_at",
                run_id,
            )
            # Whatever order won: at most one restart, and the ledger says exactly what happened.
            assert applied == (1 if write == "completed" else 0), iteration
            assert write in (None, "failed", "completed")
            assert status in ("cancelled", "succeeded")
            if status == "cancelled":
                assert flag is not None
            elif flag is not None:
                # ADR-0006 §4 linearisation: the final answer was received before the stop
                # committed, so finishing is not a further action; the request stays recorded.
                assert last_call < flag
            assert not stopped_then_acted(env, run_id)
            seen[f"{status}/{write or 'no-entry'}"] += 1
            await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
    report(f"stop vs TX5 outcomes over 50 runs: {dict(seen)}")


# --- stops while a write is in flight ------------------------------------------------------------


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
@pytest.mark.parametrize("answer", ["ok", "drop"])
@pytest.mark.asyncio
async def test_a_stop_while_a_write_is_in_flight_never_recalls_it_and_takes_no_new_action(
    env: Env, stop: str, answer: str
) -> None:
    worker, timers = await env.worker(), await env.worker()
    try:
        for iteration in range(10):
            gate = asyncio.Event()
            reset_target(gate)
            TARGET.script[:] = [answer]
            run_id = await env.submit("write")
            running = asyncio.create_task(env.drain(worker))
            await wait_until(lambda: TARGET.in_flight == 1)
            if stop == "cancel":
                assert (await env.cancel(run_id)).status_code == 202
            else:
                env.sql(
                    "UPDATE tether.runs SET deadline_at = clock_timestamp() WHERE id = %s"
                    " RETURNING id",
                    run_id,
                )
                env.sql(
                    "UPDATE tether.jobs SET run_after = clock_timestamp()"
                    " WHERE run_id = %s AND kind = 'run_deadline' RETURNING id",
                    run_id,
                )
                await env.drain(timers)
            gate.set()
            await running
            await env.drain(worker)
            expected = "cancelled" if stop == "cancel" else "timed_out"
            assert TARGET.sends == 1, iteration  # dispatched writes are never recalled or re-sent
            assert len(TARGET.effects) == 1
            if answer == "ok":
                assert env.status(run_id) == expected
                assert [s for _, _, s, _, _ in env.ledger(run_id)] == ["completed"]
            else:
                # Unknown and the run is stopped: no re-send; a human resolves it, then it ends.
                assert env.status(run_id) == "awaiting_reconciliation"
                [(entry_id,)] = env.sql(
                    "SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id
                )
                assert (await env.reconcile(entry_id, "applied")).status_code == 200
                await env.drain(worker)
                assert env.status(run_id) == expected
            assert env.model_calls(run_id) == 1  # no model call after the stop
            await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
        await timers.close()


# --- C-07: lease expiry with the write in flight -------------------------------------------------


@pytest.mark.parametrize("repeat", range(10))
@pytest.mark.asyncio
async def test_c07_overlapping_same_key_sends_apply_once(env: Env, repeat: int) -> None:
    first, second = await env.worker(), await env.worker()
    try:
        gate = asyncio.Event()
        reset_target(gate)
        run_id = await env.submit("write")
        slow = asyncio.create_task(env.drain(first))
        await wait_until(lambda: TARGET.in_flight == 1)
        env.expire_leases()  # the slow send outlives its lease; another worker takes the job
        racing = asyncio.create_task(env.drain(second))
        await wait_until(lambda: TARGET.in_flight == 2)
        gate.set()
        await asyncio.gather(slow, racing)
        await env.drain(first, second)
        assert env.status(run_id) == "succeeded"
        assert TARGET.sends == 2
        assert len(set(TARGET.keys)) == 1  # the same key on both sends
        assert len(TARGET.effects) == 1  # applied once
        assert env.events(run_id).count("TOOL_OUTCOME") == 1  # one outcome recorded
        assert [(s, a) for _, _, s, a, _ in env.ledger(run_id)] == [("completed", 2)]
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, first)
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("repeat", range(5))
@pytest.mark.asyncio
async def test_c07_f7b_lease_expiry_on_a_non_idempotent_write_is_indeterminate_never_resent(
    env: Env, repeat: int
) -> None:
    first, second = await env.worker(), await env.worker()
    try:
        gate = asyncio.Event()
        reset_target(gate)
        run_id = await env.submit("nonidem")
        slow = asyncio.create_task(env.drain(first))
        await wait_until(lambda: TARGET.in_flight == 1)
        env.expire_leases()
        await env.drain(second)  # takes the job, may not send again: indeterminate
        assert TARGET.sends == 1
        gate.set()
        await slow  # the original send finishes late; its result cannot overwrite the record
        await env.drain(first, second)
        assert TARGET.sends == 1
        assert TARGET.nonidem_effects == 1
        assert env.status(run_id) == "awaiting_reconciliation"
        assert [(s, a) for _, _, s, a, _ in env.ledger(run_id)] == [("indeterminate", 1)]
        [(entry_id,)] = env.sql("SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id)
        assert (await env.reconcile(entry_id, "applied")).status_code == 200
        await env.drain(first)
        assert env.status(run_id) == "succeeded"
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, first)
    finally:
        await first.close()
        await second.close()


# --- the approval-expiry race --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_vs_expiry_race_repeated_30_times(env: Env) -> None:
    worker = await env.worker()
    seen: Counter[str] = Counter()
    try:
        for iteration in range(30):
            before = await restarts()
            run_id, approval_id = await to_approval(env, worker)
            # The approval lapses in ~150 ms; its timer is due then.
            env.sql(
                "UPDATE tether.approvals SET expires_at = clock_timestamp() + interval '150 ms'"
                " WHERE id = %s RETURNING id",
                approval_id,
            )
            env.sql(
                "UPDATE tether.jobs SET run_after = clock_timestamp() + interval '150 ms'"
                " WHERE ref_id = %s AND kind = 'expire_approval' RETURNING id",
                approval_id,
            )

            async def approve(approval: Any = approval_id) -> int:
                await jitter(250)
                return (await env.approve(approval)).status_code

            async def expire() -> None:
                await jitter(250)
                await env.drain(worker)

            approved, _ = await asyncio.gather(approve(), expire())
            await asyncio.sleep(0.2)  # past the expiry: the timer is due
            await env.drain(worker)
            [(approval_status,)] = env.sql(
                "SELECT status FROM tether.approvals WHERE id = %s", approval_id
            )
            applied = await restarts() - before
            write = {t: s for _, t, s, _, _ in env.ledger(run_id)}.get("restart_service")
            assert (approved, approval_status) in {(200, "approved"), (409, "expired")}, iteration
            # An approval acts only while live: an expired one never authorizes the write.
            if approval_status == "expired":
                assert write is None
            assert applied == (1 if write == "completed" else 0)
            assert env.status(run_id) == "succeeded"  # the model got a typed result and finished
            seen[f"{approval_status}/{write or 'not-executed'}"] += 1
            await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
    report(f"approve vs expiry outcomes over 30 runs: {dict(seen)}")


# --- stale fencing tokens ------------------------------------------------------------------------


@pytest.mark.parametrize("repeat", range(10))
@pytest.mark.asyncio
async def test_a_driver_that_lost_its_lock_is_fenced_and_writes_nothing(
    env: Env, repeat: int
) -> None:
    gate = asyncio.Event()
    provider = GatedProvider(env.tmp_path, gate)
    zombie, healthy = await env.worker(provider), await env.worker()
    try:
        run_id = await env.submit("write")
        stuck = asyncio.create_task(process_next_job(zombie))
        await wait_until(lambda: provider.waiting == 1)  # mid-run, holding the run lock
        # Its lock connection dies (a network partition): Postgres releases the advisory lock,
        # the lease expires, and a healthy worker takes the run over with a newer token.
        with psycopg.connect(env.db.superuser_dsn(env.db.dbname), autocommit=True) as admin:
            [(pid,)] = admin.execute(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
            ).fetchall()
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        env.expire_leases()
        await env.drain(healthy)
        assert env.status(run_id) == "succeeded"
        events_before = env.events(run_id)
        calls_before = env.model_calls(run_id)
        gate.set()  # the zombie wakes up with its stale token
        await asyncio.gather(stuck, return_exceptions=True)
        await env.drain(healthy)
        assert env.events(run_id) == events_before  # it wrote nothing
        assert env.model_calls(run_id) == calls_before == 2
        assert env.status(run_id) == "succeeded"
        assert TARGET.sends == 1
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, healthy)
    finally:
        await zombie.close()
        await healthy.close()


def test_harness_terminal_statuses_cover_the_schema() -> None:
    # The terminal-run checks above use this set; keep it equal to the runtime's.
    from tether.core.statuses import TERMINAL_RUN_STATUSES

    assert {s.value for s in TERMINAL_RUN_STATUSES} == TERMINAL
