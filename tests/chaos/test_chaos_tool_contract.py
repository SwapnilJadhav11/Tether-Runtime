"""W3.2 under crashes (ADR-0021): ``fail_run`` (T-04) and target-enforced preconditions (T-21).

The process dies right after a critical transaction commits; a fresh worker takes over once the
dead worker's leases expire. A read configured to fail the run still ends ``failed`` exactly once,
with no skip events and no further model call; a stale compare-and-swap is still ``not_applied``
with a single send and no change at the target.
"""

from __future__ import annotations

import uuid

import pytest
from tests.chaos.harness import TARGET, TERMINAL, Env, crash_after_commit, is_crash

from tether.runtime.driver import WorkerRuntime

pytestmark = pytest.mark.chaos


async def drive(env: Env, run_id: uuid.UUID) -> tuple[int, WorkerRuntime]:
    """Drain, replacing the worker after every crash, until the run is terminal."""
    crashes = 0
    worker = await env.worker()
    for _ in range(30):
        try:
            await env.drain(worker)
        except BaseException as error:
            if not is_crash(error):
                raise
            crashes += 1
            await worker.close()
            env.expire_leases()
            worker = await env.worker()
            continue
        if env.status(run_id) in TERMINAL:
            return crashes, worker
        raise AssertionError(f"run stuck in {env.status(run_id)}")
    raise AssertionError("run did not finish")


def outcome_class(env: Env, run_id: uuid.UUID) -> str:
    [(found,)] = env.sql(
        "SELECT outcome_class FROM tether.ledger_entries WHERE run_id = %s", run_id
    )
    return str(found)


FAIL_RUN_POINTS = [
    ("POLICY_DECISION", 0),
    ("EXECUTION_AUTHORIZED", 0),
    ("DISPATCHED", 0),
    ("REDISPATCHED", 0),
    ("TOOL_OUTCOME", 0),
    ("RUN_FINISHED", 0),
]


@pytest.mark.parametrize(
    ("event", "skip"), FAIL_RUN_POINTS, ids=[f"{e}-{s}" for e, s in FAIL_RUN_POINTS]
)
@pytest.mark.asyncio
async def test_t04_fail_run_survives_a_crash_after_each_commit(
    env: Env, monkeypatch: pytest.MonkeyPatch, event: str, skip: int
) -> None:
    crash_after_commit(monkeypatch, event, skip=skip)
    run_id = await env.submit("strict_read")
    crashes, worker = await drive(env, run_id)
    try:
        assert crashes == 1
        [(summary,)] = env.sql("SELECT outcome_summary FROM tether.runs WHERE id = %s", run_id)
        assert (env.status(run_id), summary) == ("failed", "tool_exhausted:probe_read_strict")
        [(ordinal, tool, state, attempts, _)] = env.ledger(run_id)
        assert (ordinal, tool, state) == (0, "probe_read_strict", "failed")
        assert attempts <= 3
        assert outcome_class(env, run_id) == "unknown"
        assert "CALL_SKIPPED" not in env.events(run_id)
        assert env.model_calls(run_id) == 1  # the terminating result: no further model call
        assert not TARGET.effects  # the write of the same turn was never governed
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


PRECONDITION_POINTS = [("DISPATCHED", 0), ("TOOL_OUTCOME", 0)]


@pytest.mark.parametrize(
    ("event", "skip"), PRECONDITION_POINTS, ids=[f"{e}-{s}" for e, s in PRECONDITION_POINTS]
)
@pytest.mark.asyncio
async def test_t21_a_stale_precondition_stays_not_applied_across_a_crash(
    env: Env, monkeypatch: pytest.MonkeyPatch, event: str, skip: int
) -> None:
    crash_after_commit(monkeypatch, event, skip=skip)
    run_id = await env.submit("cas_stale")
    crashes, worker = await drive(env, run_id)
    try:
        assert crashes == 1
        assert env.status(run_id) == "succeeded"
        [(ordinal, tool, state, _, _)] = env.ledger(run_id)
        assert (ordinal, tool, state) == (0, "probe_cas", "failed")
        assert outcome_class(env, run_id) == "not_applied"
        assert TARGET.cas_version == 7  # nothing changed at the target
        assert not TARGET.effects
        assert TARGET.sends == 1  # the rejection is never re-sent
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
