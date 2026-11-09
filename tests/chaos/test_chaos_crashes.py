"""C-01, C-02, C-04, C-05, C-06 and TX12 under crashes (bounded PD-24, in process).

A process dies right after each critical transaction commits, during a model call, and with a
write in flight. A fresh worker then takes over once the dead worker's leases expire. Every run
must finish exactly once, every write must apply at most once, and the authoritative record must
stay complete (T-12) with a valid audit chain.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from tests.chaos.harness import (
    TARGET,
    TERMINAL,
    Crash,
    CrashingProvider,
    Env,
    crash_after_commit,
    is_crash,
    restarts,
)

from tether.runtime.driver import WorkerRuntime
from tether.testing.scripted import ScriptedProvider
from tether.worker.handlers import execute_tool

pytestmark = pytest.mark.chaos


async def drive_through_crashes(
    env: Env,
    run_id: uuid.UUID | None,
    *,
    scenario: str = "remediate",
    provider: Callable[[], ScriptedProvider] | None = None,
    max_crashes: int = 20,
) -> tuple[uuid.UUID, int, WorkerRuntime]:
    """Submit (unless given), approve when asked, and replace the worker after every crash."""
    crashes = 0

    async def fresh() -> WorkerRuntime:
        return await env.worker(provider() if provider is not None else None)

    if run_id is None:
        try:
            run_id = await env.submit(scenario)
        except BaseException as error:
            if not is_crash(error):
                raise
            crashes += 1  # the api died after TX1 committed: the run exists, the client lost it
            [(run_id,)] = env.sql("SELECT id FROM tether.runs ORDER BY created_at DESC LIMIT 1")
    worker = await fresh()
    approved = False
    for _ in range(60):
        try:
            await env.drain(worker)
        except BaseException as error:
            if not is_crash(error):
                raise
            crashes += 1
            assert crashes <= max_crashes
            await worker.close()
            env.expire_leases()
            worker = await fresh()
            continue
        status = env.status(run_id)
        if status in TERMINAL or status == "awaiting_reconciliation":
            return run_id, crashes, worker
        if status == "awaiting_approval" and not approved:
            approved = True
            try:
                assert (await env.approve(env.approval_id(run_id))).status_code == 200
            except BaseException as error:
                if not is_crash(error):
                    raise
                crashes += 1  # TX4 committed; the response was lost with the api
            continue
        raise AssertionError(f"run stuck in {status}")
    raise AssertionError("run did not finish")


# Every critical transaction of the governed flow: (audit event that marks it, occurrences to
# skip). The process dies right after it commits, before the checkpoint, job completion or reply.
COMMIT_POINTS = [
    ("RUN_SUBMITTED", 0),  # TX1 (api)
    ("POLICY_DECISION", 0),  # TX2, the read
    ("EXECUTION_AUTHORIZED", 0),  # TX5, the read
    ("DISPATCHED", 0),  # TX6, the read
    ("TOOL_OUTCOME", 0),  # TX7, the read
    ("POLICY_DECISION", 1),  # TX2, the write
    ("APPROVAL_REQUESTED", 0),  # TX3
    ("APPROVAL_DECIDED", 0),  # TX4 (api)
    ("EXECUTION_AUTHORIZED", 1),  # TX5, the write (C-04: after approval, before dispatch)
    ("DISPATCHED", 1),  # TX6, the write (C-04/C-05 boundary)
    ("TOOL_OUTCOME", 1),  # TX7, the write
    ("RUN_FINISHED", 0),  # TX11
]


@pytest.mark.parametrize(
    ("event", "skip"), COMMIT_POINTS, ids=[f"{e}-{s}" for e, s in COMMIT_POINTS]
)
@pytest.mark.asyncio
async def test_c01_c04_a_crash_after_each_commit_resumes_without_duplicates(
    env: Env, monkeypatch: pytest.MonkeyPatch, event: str, skip: int
) -> None:
    crash_after_commit(monkeypatch, event, skip=skip)
    run_id, crashes, worker = await drive_through_crashes(env, None)
    try:
        assert crashes == 1  # the injection fired exactly once
        assert env.status(run_id) == "succeeded"
        assert await restarts() == 1  # the approved write applied exactly once
        assert [(o, t, s) for o, t, s, _, _ in env.ledger(run_id)] == [
            (0, "get_service_health", "completed"),
            (1, "restart_service", "completed"),
        ]
        assert env.model_calls(run_id) == 3
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.parametrize("turn", [0, 1, 2])
@pytest.mark.asyncio
async def test_c02_a_crash_during_a_model_call_re_calls_the_turn(env: Env, turn: int) -> None:
    provider = CrashingProvider(env.tmp_path, turn=turn, times=1)
    run_id, crashes, worker = await drive_through_crashes(env, None, provider=lambda: provider)
    try:
        assert crashes == 1
        assert env.status(run_id) == "succeeded"
        assert await restarts() == 1
        # The interrupted attempt left no row: model_calls are written when a call returns.
        assert env.model_calls(run_id) == 3
        await env.assert_reconstructable(run_id)
    finally:
        await worker.close()


@pytest.mark.parametrize("step", ["crash_before", "crash_after"])
@pytest.mark.parametrize("repeat", range(5))
@pytest.mark.asyncio
async def test_c05_c06_a_crash_with_an_idempotent_write_in_flight_applies_it_once(
    env: Env, step: str, repeat: int
) -> None:
    TARGET.script[:] = [step]
    run_id, crashes, worker = await drive_through_crashes(env, None, scenario="write")
    try:
        assert crashes == 1
        assert env.status(run_id) == "succeeded"
        # Redelivered with the same key: one effect, however many sends.
        assert TARGET.sends == 2
        assert len(set(TARGET.keys)) == 1
        assert len(TARGET.effects) == 1
        assert [(s, a) for _, _, s, a, _ in env.ledger(run_id)] == [("completed", 2)]
        assert env.events(run_id).count("REDISPATCHED") == 1
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.parametrize("outcome", ["applied", "not_applied"])
@pytest.mark.asyncio
async def test_c07_a_crash_with_a_non_idempotent_write_in_flight_is_held_then_reconciled(
    env: Env, outcome: str
) -> None:
    TARGET.script[:] = ["crash_before"]
    run_id = await env.submit("nonidem")
    _, crashes, worker = await drive_through_crashes(env, run_id, scenario="nonidem")
    try:
        # Never re-sent (it could duplicate the effect): unknown means indeterminate, held.
        assert crashes == 1
        assert TARGET.sends == 1
        assert env.status(run_id) == "awaiting_reconciliation"
        [(_, _, state, attempts, _)] = env.ledger(run_id)
        assert (state, attempts) == ("indeterminate", 1)
        await env.assert_reconstructable(run_id)
        [(entry_id,)] = env.sql("SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id)
        assert (await env.reconcile(entry_id, outcome)).status_code == 200
        await env.drain(worker)
        assert env.status(run_id) == "succeeded"
        assert TARGET.sends == 1
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_tx12_advance_run_crashing_on_every_delivery_is_dead_lettered(env: Env) -> None:
    provider = CrashingProvider(env.tmp_path, turn=0, times=100)
    run_id, crashes, worker = await drive_through_crashes(env, None, provider=lambda: provider)
    try:
        assert crashes == 8  # PD-14: advance_run is delivered at most 8 times
        assert env.status(run_id) == "failed"
        assert env.events(run_id)[-2:] == ["DEAD_LETTERED", "RUN_FINISHED"]
        assert env.model_calls(run_id) == 0
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_tx12_execute_tool_crashing_before_dispatch_is_definitely_not_applied(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash_before_tx6(*args: object, **kwargs: object) -> object:
        raise Crash("killed before TX6")

    monkeypatch.setattr(execute_tool, "_prepare", crash_before_tx6)
    run_id = await env.submit("write")
    _, crashes, worker = await drive_through_crashes(env, run_id, scenario="write")
    try:
        assert crashes == 5  # PD-14: execute_tool is delivered at most 5 times
        assert TARGET.sends == 0
        [(_, _, state, attempts, _)] = env.ledger(run_id)
        assert (state, attempts) == ("failed", 0)  # never dispatched: definitely not applied
        assert env.sql(
            "SELECT state FROM tether.jobs WHERE run_id = %s AND kind = 'execute_tool'", run_id
        ) == [("dead",)]
        assert env.status(run_id) == "succeeded"  # the model got a typed failure and finished
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_a_send_budget_spent_by_crashes_is_indeterminate_then_reconciled(env: Env) -> None:
    # Three crashes with the write in flight spend the PD-14 send budget (3): the next delivery
    # may not send again, so the outcome is final: indeterminate, held for a human.
    TARGET.script[:] = ["crash_before", "crash_before", "crash_before"]
    run_id = await env.submit("write")
    _, crashes, worker = await drive_through_crashes(env, run_id, scenario="write")
    try:
        assert crashes == 3
        assert TARGET.sends == 3
        assert len(set(TARGET.keys)) == 1
        assert not TARGET.effects
        assert env.status(run_id) == "awaiting_reconciliation"
        [(entry_id,)] = env.sql("SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id)
        assert (await env.reconcile(entry_id, "not_applied")).status_code == 200
        await env.drain(worker)
        assert env.status(run_id) == "succeeded"
        assert TARGET.sends == 3
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
