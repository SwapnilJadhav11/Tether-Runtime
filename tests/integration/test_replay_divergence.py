"""W2.2: hash-checked create-or-get and ``REPLAY_DIVERGENCE`` (ADR-0003 rule 4, ADR-0005 rule 6,
architecture invariant 3, AD-10).

Every path that adopts persisted state for a call (the decision event, the approval row, the
ledger row, and the rows the wait nodes re-read) recomputes the call hash from the current call
under the run's pinned binding key and compares it with the stored one. A match preserves the
existing replay behaviour. A mismatch is an integrity failure: a ``REPLAY_DIVERGENCE`` audit event
is written, nothing new is authorized or sent, no stored row is changed, and the run ends
``failed`` with the typed reason ``replay_divergence`` only once every unresolved ledger entry
has drained to an outcome or a reconciliation.

Node-end replays are produced with the chaos harness: the worker dies right after a critical
transaction commits and before LangGraph writes the checkpoint, so the next driver re-runs the
node and must adopt (or refuse) the persisted rows.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from services.demo_target_service.app import create_app as create_demo_target
from tests.chaos.harness import (
    TARGET,
    Env,
    crash_after_commit,
    is_crash,
    make_env,
    reset_target,
    restarts,
    wait_until,
)
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.core.ids import WorkspaceId
from tether.core.statuses import JobKind
from tether.queue import backoff
from tether.runtime.driver import WorkerRuntime
from tether.runtime.graph import REPLAY_DIVERGENCE_REASON, call_hash_for
from tether.storage.repos.audit import AuditRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork
from tether.worker.main import process_next_job

WS = "ws-a"
OTHER_HASH = "f" * 64
SCOPE = WorkspaceScope(WorkspaceId(WS))


@pytest.fixture(autouse=True)
def demo_target(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    reset_target()
    # Retries are immediate: the backoff itself is covered by the retry tests.
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
    yield
    demo_client.set_transport(None)


@pytest_asyncio.fixture
async def env(seeded: Any, tmp_path: Path) -> AsyncIterator[Env]:
    yield make_env(seeded.db, tmp_path)


@pytest_asyncio.fixture
async def worker(env: Env) -> AsyncIterator[WorkerRuntime]:
    runtime = await env.worker()
    try:
        yield runtime
    finally:
        await runtime.close()


def run_row(env: Env, run_id: uuid.UUID) -> tuple[Any, ...]:
    """(status, outcome_summary) of the run."""
    return env.sql("SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


def divergence_payloads(env: Env, run_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = env.sql(
        "SELECT payload_masked FROM tether.audit_events"
        " WHERE run_id = %s AND event_type = 'REPLAY_DIVERGENCE' ORDER BY seq",
        run_id,
    )
    return [payload for (payload,) in rows]


def ledger_hashes(env: Env, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    """(id, call_hash, state, outcome_reason) per entry."""
    return env.sql(
        "SELECT id, call_hash, state, outcome_reason FROM tether.ledger_entries"
        " WHERE run_id = %s ORDER BY step_ordinal",
        run_id,
    )


def add_wake(env: Env, run_id: uuid.UUID) -> None:
    env.sql(
        "INSERT INTO tether.jobs (id, kind, workspace_id, run_id, max_attempts)"
        " VALUES (%s, 'advance_run', %s, %s, 8) RETURNING id",
        uuid.uuid4(),
        WS,
        run_id,
    )


def set_ledger_hash(env: Env, run_id: uuid.UUID, call_hash: str) -> Any:
    [(entry_id,)] = env.sql(
        "UPDATE tether.ledger_entries SET call_hash = %s WHERE run_id = %s RETURNING id",
        call_hash,
        run_id,
    )
    return entry_id


async def seed_event(
    env: Env, worker: WorkerRuntime, run_id: uuid.UUID, dedupe: str, event_type: str, **payload: Any
) -> None:
    """An event as an earlier invocation would have recorded it (properly chained)."""
    tool = payload.pop("tool", None)
    async with UnitOfWork(worker.engine) as uow:
        await uow.repository(AuditRepository, SCOPE).append(
            worker.keys,
            run_id=run_id,
            dedupe_key=dedupe,
            event_type=event_type,
            payload=payload,
            tool=tool,
        )


def expected_hash(
    env: Env, worker: WorkerRuntime, run_id: uuid.UUID, tool: str, **args: Any
) -> str:
    """The call hash the run driver computes for ``tool(**args)`` on this run."""
    [(workspace_id, agent_id, binding_key_id)] = env.sql(
        "SELECT workspace_id, agent_id, binding_key_id FROM tether.runs WHERE id = %s", run_id
    )
    descriptor = worker.registries[WORKLOAD.name].resolve(tool)
    assert descriptor is not None
    run = {
        "workspace_id": workspace_id,
        "id": run_id,
        "agent_id": agent_id,
        "binding_key_id": binding_key_id,
    }
    return call_hash_for(worker.keys, run, descriptor, descriptor.input_model(**args))


async def drain_until_crash(env: Env, worker: WorkerRuntime) -> None:
    """Drain, expecting the injected crash; the dead worker's leases are then expired."""
    try:
        await env.drain(worker)
    except BaseException as error:
        if not is_crash(error):
            raise
    else:
        raise AssertionError("the injected crash did not fire")
    await worker.close()
    env.expire_leases()


async def assert_failed_closed(env: Env, worker: WorkerRuntime, run_id: uuid.UUID) -> None:
    assert run_row(env, run_id) == ("failed", REPLAY_DIVERGENCE_REASON)
    await assert_evidence_kept(env, worker, run_id)


async def assert_evidence_kept(env: Env, worker: WorkerRuntime, run_id: uuid.UUID) -> None:
    """The divergence stays in the audit chain and the run is terminal and stable."""
    events = env.events(run_id)
    assert events.count("REPLAY_DIVERGENCE") == 1
    assert events[-1] == "RUN_FINISHED"
    assert len(divergence_payloads(env, run_id)) == 1
    await env.assert_reconstructable(run_id)
    await env.assert_terminal_stable(run_id, worker)


def pass_deadline(env: Env, run_id: uuid.UUID) -> None:
    """The stored deadline is now in the past: the stop predicate is true by database time."""
    env.sql(
        "UPDATE tether.runs SET deadline_at = clock_timestamp() WHERE id = %s RETURNING id", run_id
    )


# --- A. decision adoption -------------------------------------------------------------------


@pytest.mark.parametrize("stored", [OTHER_HASH, None], ids=["different-hash", "invalid-no-hash"])
@pytest.mark.asyncio
async def test_a_persisted_decision_with_a_different_hash_is_not_adopted(
    env: Env, worker: WorkerRuntime, stored: str | None
) -> None:
    run_id = await env.submit("write")
    await seed_event(
        env,
        worker,
        run_id,
        "decision:0",
        "POLICY_DECISION" if stored is not None else "CALL_INVALID",
        decision="allow" if stored is not None else "invalid",
        reasons=["recorded earlier"],
        call_hash=stored,
        tool="probe_write",
    )
    await env.drain(worker)
    # Fail closed: no authorization, no ledger row, nothing sent, the stored event untouched.
    assert "EXECUTION_AUTHORIZED" not in env.events(run_id)
    assert env.ledger(run_id) == []
    assert TARGET.sends == 0
    assert env.model_calls(run_id) == 1
    [payload] = divergence_payloads(env, run_id)
    assert (payload["step_ordinal"], payload["path"]) == (0, "decision")
    assert payload["stored_call_hash"] == stored
    assert payload["recomputed_call_hash"] == expected_hash(
        env, worker, run_id, "probe_write", target="a"
    )
    [(decision_hash,)] = env.sql(
        "SELECT payload_masked->>'call_hash' FROM tether.audit_events"
        " WHERE run_id = %s AND dedupe_key = 'decision:0'",
        run_id,
    )
    assert decision_hash == stored  # never regenerated to match
    await assert_failed_closed(env, worker, run_id)


# --- D. positive replay -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d_a_persisted_decision_with_the_matching_hash_is_adopted(
    env: Env, worker: WorkerRuntime
) -> None:
    run_id = await env.submit("write")
    matching = expected_hash(env, worker, run_id, "probe_write", target="a")
    await seed_event(
        env,
        worker,
        run_id,
        "decision:0",
        "POLICY_DECISION",
        decision="deny",
        reasons=["recorded earlier"],
        call_hash=matching,
        tool="probe_write",
    )
    await env.drain(worker)
    # Adopted, not re-decided: the earlier deny stands and the write is never authorized.
    assert run_row(env, run_id) == ("succeeded", "written")
    assert env.ledger(run_id) == []
    assert TARGET.sends == 0
    assert "REPLAY_DIVERGENCE" not in env.events(run_id)
    assert env.events(run_id).count("POLICY_DECISION") == 1
    await env.assert_reconstructable(run_id)


# --- B. approval adoption -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_an_approval_row_for_a_different_hash_is_not_adopted_on_node_end_replay(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker dies right after TX3 commits; the approval row is then for another hash."""
    crash_after_commit(monkeypatch, "APPROVAL_REQUESTED")
    run_id = await env.submit("remediate")
    await drain_until_crash(env, await env.worker())
    env.sql(
        "UPDATE tether.approvals SET call_hash = %s WHERE run_id = %s RETURNING id",
        OTHER_HASH,
        run_id,
    )
    fresh = await env.worker()
    try:
        await env.drain(fresh)
        assert await restarts() == 0
        [(approval_count, stored)] = env.sql(
            "SELECT count(*), min(call_hash) FROM tether.approvals WHERE run_id = %s", run_id
        )
        assert (approval_count, stored) == (1, OTHER_HASH)  # no replacement, no rewrite
        assert [t for _, t, _, _, _ in env.ledger(run_id)] == ["get_service_health"]
        [payload] = divergence_payloads(env, run_id)
        assert (payload["step_ordinal"], payload["path"]) == (1, "approval")
        await assert_failed_closed(env, fresh, run_id)
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_b_an_approved_approval_for_a_different_hash_never_authorizes_the_write(
    env: Env, worker: WorkerRuntime
) -> None:
    run_id = await env.submit("remediate")
    await env.drain(worker)
    assert env.status(run_id) == "awaiting_approval"
    approval_id = env.approval_id(run_id)
    env.sql(
        "UPDATE tether.approvals SET call_hash = %s WHERE id = %s RETURNING id",
        OTHER_HASH,
        approval_id,
    )
    assert (await env.approve(approval_id)).status_code == 200
    await env.drain(worker)
    assert await restarts() == 0
    assert [t for _, t, _, _, _ in env.ledger(run_id)] == ["get_service_health"]
    events = env.events(run_id)
    assert "EXECUTION_AUTHORIZED" not in events[events.index("APPROVAL_DECIDED") :]
    [payload] = divergence_payloads(env, run_id)
    assert (payload["step_ordinal"], payload["path"]) == (1, "approval_wait")
    await assert_failed_closed(env, worker, run_id)


# --- C. ledger-first adoption ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_a_ledger_row_for_a_different_hash_is_not_adopted_or_overwritten(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker dies right after TX5 commits; the ledger row is then for another hash."""
    crash_after_commit(monkeypatch, "EXECUTION_AUTHORIZED")
    run_id = await env.submit("write")
    await drain_until_crash(env, await env.worker())
    entry_id = set_ledger_hash(env, run_id, OTHER_HASH)
    fresh = await env.worker()
    try:
        await env.drain(fresh)
        assert TARGET.sends == 0
        [(found, stored, state, _)] = ledger_hashes(env, run_id)
        assert (found, stored, state) == (entry_id, OTHER_HASH, "failed")  # not overwritten
        [payload] = divergence_payloads(env, run_id)
        assert (payload["step_ordinal"], payload["path"]) == (0, "ledger")
        await assert_failed_closed(env, fresh, run_id)
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_c_a_pending_entry_is_not_dispatched_once_the_run_has_diverged(
    env: Env, worker: WorkerRuntime
) -> None:
    """``execute_tool`` refuses a first send for a run with a recorded divergence."""
    run_id = await env.submit("write")
    while await process_next_job(worker, kinds=(JobKind.ADVANCE_RUN,)):
        pass
    [(_, _, state, _)] = ledger_hashes(env, run_id)
    assert state == "pending"
    await seed_event(
        env, worker, run_id, "replay_divergence:7", "REPLAY_DIVERGENCE", step_ordinal=7
    )
    while await process_next_job(worker, kinds=(JobKind.EXECUTE_TOOL,)):
        pass
    assert TARGET.sends == 0
    [(_, _, state, reason)] = ledger_hashes(env, run_id)
    assert (state, reason) == ("failed", "not applied: replay divergence")
    assert "DISPATCHED" not in env.events(run_id)


# --- E. node-end replay with matching rows preserves the existing behaviour -----------------


@pytest.mark.parametrize(
    ("event", "skip", "scenario"),
    [
        ("POLICY_DECISION", 0, "write"),
        ("EXECUTION_AUTHORIZED", 0, "write"),
        ("APPROVAL_REQUESTED", 0, "remediate"),
        ("EXECUTION_AUTHORIZED", 1, "remediate"),
    ],
    ids=["decision", "ledger", "approval-request", "ledger-after-approval"],
)
@pytest.mark.asyncio
async def test_e_node_end_replay_with_matching_hashes_adopts_the_persisted_rows(
    env: Env, monkeypatch: pytest.MonkeyPatch, event: str, skip: int, scenario: str
) -> None:
    crash_after_commit(monkeypatch, event, skip=skip)
    run_id = await env.submit(scenario)
    worker = await env.worker()
    approved = False
    for _ in range(10):
        try:
            await env.drain(worker)
        except BaseException as error:
            if not is_crash(error):
                raise
            await worker.close()
            env.expire_leases()
            worker = await env.worker()
            continue
        if env.status(run_id) == "awaiting_approval" and not approved:
            approved = True
            assert (await env.approve(env.approval_id(run_id))).status_code == 200
            continue
        break
    try:
        assert env.status(run_id) == "succeeded"
        assert "REPLAY_DIVERGENCE" not in env.events(run_id)
        if scenario == "write":
            assert TARGET.sends == 1
            assert [(s, a) for _, _, s, a, _ in env.ledger(run_id)] == [("completed", 1)]
        else:
            assert await restarts() == 1
            assert [s for _, _, s, _, _ in env.ledger(run_id)] == ["completed", "completed"]
        await env.assert_reconstructable(run_id)
    finally:
        await worker.close()


# --- F. divergence found while an external outcome is unresolved ---------------------------


@pytest.mark.asyncio
async def test_f_divergence_with_a_write_in_flight_waits_for_its_outcome_before_failing(
    env: Env,
) -> None:
    gate = asyncio.Event()
    reset_target(gate)
    sender, driver = await env.worker(), await env.worker()
    try:
        run_id = await env.submit("write")
        sending = asyncio.create_task(env.drain(sender))
        await wait_until(lambda: TARGET.in_flight == 1)  # TX6 committed, the send is in flight
        assert env.status(run_id) == "awaiting_tool"
        set_ledger_hash(env, run_id, OTHER_HASH)
        add_wake(env, run_id)  # the wait node re-reads the entry and finds another call's hash
        await env.drain(driver)
        # The unresolved truth is not hidden: the run holds and is not terminal.
        assert env.status(run_id) == "awaiting_reconciliation"
        assert "REPLAY_DIVERGENCE" in env.events(run_id)
        assert "RUN_FINISHED" not in env.events(run_id)
        [payload] = divergence_payloads(env, run_id)
        assert payload["path"] == "ledger_wait"
        gate.set()
        await sending  # TX7 records the real outcome and wakes the run
        await env.drain(driver)
        assert TARGET.sends == 1
        assert len(TARGET.effects) == 1
        assert [s for _, _, s, _, _ in env.ledger(run_id)] == ["completed"]
        events = env.events(run_id)
        assert events.index("TOOL_OUTCOME") < events.index("RUN_FINISHED")
        await assert_failed_closed(env, driver, run_id)
    finally:
        gate.set()
        await sender.close()
        await driver.close()


@pytest.mark.asyncio
async def test_f_divergence_with_a_write_in_flight_and_a_cancel_drains_then_ends_cancelled(
    env: Env,
) -> None:
    """ADR-0006 precedence: the stop decides the terminal status, after the real outcome."""
    gate = asyncio.Event()
    reset_target(gate)
    sender, driver = await env.worker(), await env.worker()
    try:
        run_id = await env.submit("write")
        sending = asyncio.create_task(env.drain(sender))
        await wait_until(lambda: TARGET.in_flight == 1)
        set_ledger_hash(env, run_id, OTHER_HASH)
        add_wake(env, run_id)
        await env.drain(driver)
        assert env.status(run_id) == "awaiting_reconciliation"
        assert (await env.cancel(run_id)).status_code == 202
        await env.drain(driver)
        # Still held: a cancel never recalls or hides the dispatched write.
        assert env.status(run_id) == "awaiting_reconciliation"
        assert "RUN_FINISHED" not in env.events(run_id)
        gate.set()
        await sending
        await env.drain(driver)
        assert len(TARGET.effects) == 1
        assert [s for _, _, s, _, _ in env.ledger(run_id)] == ["completed"]
        expected = ("cancelled", f"cancelled by alice; {REPLAY_DIVERGENCE_REASON}")
        assert run_row(env, run_id) == expected
        events = env.events(run_id)
        assert events.index("TOOL_OUTCOME") < events.index("RUN_FINISHED")
        assert "CANCEL_REQUESTED" in events
        await assert_evidence_kept(env, driver, run_id)
    finally:
        gate.set()
        await sender.close()
        await driver.close()


# --- stop precedence without unresolved work (ADR-0006) ------------------------------------


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_a_stop_visible_after_the_divergence_decides_the_terminal_status(
    env: Env, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """The worker dies right after TX-RD commits; a stop lands before the run resumes."""
    crash_after_commit(monkeypatch, "REPLAY_DIVERGENCE")
    run_id = await env.submit("write")
    worker = await env.worker()
    await seed_event(
        env,
        worker,
        run_id,
        "decision:0",
        "POLICY_DECISION",
        decision="allow",
        reasons=["recorded earlier"],
        call_hash=OTHER_HASH,
        tool="probe_write",
    )
    await drain_until_crash(env, worker)
    assert env.events(run_id)[-1] == "REPLAY_DIVERGENCE"
    if stop == "cancel":
        assert (await env.cancel(run_id)).status_code == 202
        expected = ("cancelled", f"cancelled by alice; {REPLAY_DIVERGENCE_REASON}")
    else:
        pass_deadline(env, run_id)
        expected = ("timed_out", f"deadline passed; {REPLAY_DIVERGENCE_REASON}")
    fresh = await env.worker()
    try:
        await env.drain(fresh)
        assert run_row(env, run_id) == expected
        assert TARGET.sends == 0
        assert env.ledger(run_id) == []
        assert env.model_calls(run_id) == 1
        await assert_evidence_kept(env, fresh, run_id)
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_f_divergence_with_an_indeterminate_write_fails_only_after_reconciliation(
    env: Env,
) -> None:
    gate = asyncio.Event()
    reset_target(gate)
    TARGET.script[:] = ["drop"]
    sender, driver = await env.worker(), await env.worker()
    try:
        run_id = await env.submit("nonidem")
        sending = asyncio.create_task(env.drain(sender))
        await wait_until(lambda: TARGET.in_flight == 1)
        set_ledger_hash(env, run_id, OTHER_HASH)
        add_wake(env, run_id)
        await env.drain(driver)
        assert env.status(run_id) == "awaiting_reconciliation"
        gate.set()
        await sending  # the answer is lost: indeterminate, never re-sent
        await env.drain(driver)
        assert env.status(run_id) == "awaiting_reconciliation"
        assert [s for _, _, s, _, _ in env.ledger(run_id)] == ["indeterminate"]
        assert "RUN_FINISHED" not in env.events(run_id)
        [(entry_id, *_)] = ledger_hashes(env, run_id)
        assert (await env.reconcile(entry_id, "applied")).status_code == 200
        await env.drain(driver)
        assert TARGET.sends == 1
        assert TARGET.nonidem_effects == 1
        assert [(s, r) for _, _, s, _, r in env.ledger(run_id)] == [
            ("completed", "reconciled_applied")
        ]
        events = env.events(run_id)
        assert events.index("RECONCILED") < events.index("RUN_FINISHED")
        await assert_failed_closed(env, driver, run_id)
    finally:
        gate.set()
        await sender.close()
        await driver.close()
