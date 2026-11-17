"""W5.1 under crashes and races: automatic verification (ADR-0019 rules 9-11, ADR-0022).

- A crash right after each verification transaction commits (the unknown outcome that enqueues
  the probe, the gate TX-V1, the inconclusive record, the outcome TX-V2): a fresh worker carries
  on; the write is never re-sent; the gate count always matches the probes that started; the
  entry is resolved at most once.
- Competing workers on the same ``verify_outcome`` jobs: each entry is verified exactly once.
- The verify-versus-reconcile race, repeated: exactly one resolution, and a person who commits
  first always wins (the probe's answer is then ``VERIFICATION_LATE`` evidence only).
- A stop arriving while a probe is in flight: the probe's definitive answer is recorded, and the
  run only terminates (``cancelled``), naming the verified outcome.

Every scenario ends with the extended T-12 checks (``assert_reconstructable``).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from tests.chaos.harness import (
    TARGET,
    TERMINAL,
    Env,
    crash_after_commit,
    is_crash,
    reset_target,
    wait_until,
)

from tether.queue import backoff
from tether.runtime.driver import WorkerRuntime

pytestmark = pytest.mark.chaos


@pytest.fixture(autouse=True)
def immediate_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backoff, "verify_backoff_seconds", lambda declared, attempt, rng=None: 0.0)


async def drive(env: Env, run_id: uuid.UUID, max_crashes: int = 10) -> tuple[int, WorkerRuntime]:
    """Drain, replacing the worker after every crash, until the run is terminal or held."""
    crashes = 0
    worker = await env.worker()
    for _ in range(40):
        try:
            await env.drain(worker)
        except BaseException as error:
            if not is_crash(error):
                raise
            crashes += 1
            assert crashes <= max_crashes
            await worker.close()
            env.expire_leases()
            worker = await env.worker()
            continue
        status = env.status(run_id)
        if status in TERMINAL or status == "awaiting_reconciliation":
            return crashes, worker
        raise AssertionError(f"run stuck in {status}")
    raise AssertionError("run did not finish")


def entry(env: Env, run_id: uuid.UUID) -> tuple[object, ...]:
    [row] = env.sql(
        "SELECT id, state, resolution, attempts, verification_attempts FROM tether.ledger_entries"
        " WHERE run_id = %s",
        run_id,
    )
    return row


# (event that marks the committed transaction, earlier occurrences to skip)
VERIFY_POINTS = [
    ("TOOL_OUTCOME", 0),  # TX7: the unknown outcome commits with the verify_outcome job
    ("VERIFICATION_ATTEMPTED", 0),  # TX-V1: the gate commits, the probe has not run
    ("VERIFIED", 0),  # TX-V2: resolved, the job is not yet completed
]


@pytest.mark.parametrize(("event", "skip"), VERIFY_POINTS, ids=[e for e, _ in VERIFY_POINTS])
@pytest.mark.asyncio
async def test_t27_a_crash_after_each_verification_commit_resolves_exactly_once(
    env: Env, monkeypatch: pytest.MonkeyPatch, event: str, skip: int
) -> None:
    TARGET.script[:] = ["drop"]  # applied, the answer lost: unknown, never re-sent
    crash_after_commit(monkeypatch, event, skip=skip)
    run_id = await env.submit("verified")
    crashes, worker = await drive(env, run_id)
    try:
        assert crashes == 1
        assert env.status(run_id) == "succeeded"
        _, state, resolution, attempts, verification_attempts = entry(env, run_id)
        assert (state, resolution, attempts) == ("completed", "verified_applied", 1)
        assert TARGET.sends == 1  # the write itself: never re-sent
        # A crash after the gate costs one counted attempt; every counted attempt is one probe
        # that started or a gate whose probe died with its worker.
        expected = 2 if event == "VERIFICATION_ATTEMPTED" else 1
        assert verification_attempts == expected
        assert TARGET.probes == 1
        assert env.events(run_id).count("VERIFIED") == 1
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_t27_a_crash_after_an_inconclusive_record_continues_to_the_bound(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    TARGET.script[:] = ["drop"]
    TARGET.probe_script[:] = ["inconclusive", "inconclusive", "inconclusive"]
    crash_after_commit(monkeypatch, "VERIFICATION_INCONCLUSIVE")
    run_id = await env.submit("verified")
    crashes, worker = await drive(env, run_id)
    try:
        assert crashes == 1
        assert env.status(run_id) == "awaiting_reconciliation"
        _, state, resolution, _, verification_attempts = entry(env, run_id)
        assert (state, resolution, verification_attempts) == ("indeterminate", None, 3)
        assert env.events(run_id).count("VERIFICATION_EXHAUSTED") == 1
        assert TARGET.sends == 1
        await env.assert_reconstructable(run_id)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_t27_competing_workers_verify_each_entry_exactly_once(env: Env) -> None:
    runs = []
    for _ in range(4):
        TARGET.script.append("drop")
        runs.append(await env.submit("verified"))
    workers = [await env.worker() for _ in range(3)]
    try:
        await env.drain(*workers)
        for run_id in runs:
            assert env.status(run_id) == "succeeded"
            _, _, resolution, attempts, verification_attempts = entry(env, run_id)
            assert (resolution, attempts, verification_attempts) == ("verified_applied", 1, 1)
            assert env.events(run_id).count("VERIFIED") == 1
            await env.assert_reconstructable(run_id)
        assert TARGET.sends == len(runs)
        assert TARGET.probes == len(runs)
    finally:
        for worker in workers:
            await worker.close()


async def _race_once(env: Env, *, human_first: bool, delay: float) -> uuid.UUID:
    """Set up an indeterminate entry whose probe is held in flight, then race a reconciliation."""
    gate = asyncio.Event()
    TARGET.probe_gate = gate
    TARGET.script[:] = ["drop"]
    run_id = await env.submit("verified")
    worker = await env.worker()
    try:
        draining = asyncio.ensure_future(env.drain(worker))
        await wait_until(lambda: TARGET.probing == 1)
        entry_id = entry(env, run_id)[0]

        async def person() -> int:
            await asyncio.sleep(delay if not human_first else 0)
            return (await env.reconcile(entry_id, "not_applied")).status_code

        async def probe() -> None:
            await asyncio.sleep(0 if not human_first else delay)
            gate.set()

        codes, _ = await asyncio.gather(person(), probe())
        await draining
        await env.drain(worker)
        assert codes in (200, 409)
        return run_id
    finally:
        gate.set()
        await worker.close()


@pytest.mark.parametrize("repeat", range(20))
@pytest.mark.asyncio
async def test_t27_the_verify_reconcile_race_resolves_once_and_a_first_person_wins(
    env: Env, repeat: int
) -> None:
    reset_target()
    # Deterministic jitter (as the other races): a failure replays the same schedule.
    digest = hashlib.sha256(f"tether-verify-race:{repeat}".encode()).digest()
    delay = int.from_bytes(digest[:4]) % 51 / 1000
    run_id = await _race_once(env, human_first=repeat % 2 == 0, delay=delay)
    trail = env.events(run_id)
    _, state, resolution, attempts, _ = entry(env, run_id)
    assert attempts == 1
    assert TARGET.sends == 1
    # Exactly one resolution, recorded once, by whoever committed first.
    assert (trail.count("RECONCILED"), trail.count("VERIFIED")) in ((1, 0), (0, 1))
    if trail.count("RECONCILED"):
        assert (state, resolution) == ("failed", "reconciled_not_applied")
        assert trail.count("VERIFICATION_LATE") == 1  # the probe's answer: evidence only
    else:
        assert (state, resolution) == ("completed", "verified_applied")
    assert env.status(run_id) == "succeeded"
    await env.assert_reconstructable(run_id)


@pytest.mark.asyncio
async def test_t27_a_stop_during_a_probe_keeps_its_answer_and_only_terminates(env: Env) -> None:
    gate = asyncio.Event()
    TARGET.probe_gate = gate
    TARGET.script[:] = ["drop"]
    run_id = await env.submit("verified")
    worker = await env.worker()
    try:
        draining = asyncio.ensure_future(env.drain(worker))
        await wait_until(lambda: TARGET.probing == 1)
        calls = env.model_calls(run_id)
        assert (await env.cancel(run_id)).status_code == 202
        gate.set()
        await draining
        await env.drain(worker)
        _, _, resolution, _, _ = entry(env, run_id)
        assert resolution == "verified_applied"
        [(status, summary)] = env.sql(
            "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id
        )
        assert status == "cancelled"
        assert "probe_verified verified_applied" in summary
        assert env.model_calls(run_id) == calls  # no further model call after the stop
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        gate.set()
        await worker.close()
