"""W2.3: audited ``skipped`` calls (ADR-0012 rule 4, ADR-0005 rule 3, architecture invariant 32).

When call k of a multi-call model turn ends with any result other than ``applied``, every remaining
call of the turn receives a typed ``skipped`` result. Each skip is a decision-class audit event
``CALL_SKIPPED`` for its own ordinal: create-or-get (``skipped:<ordinal>``), fenced, written after
call k's own outcome and before anything that consumes the skipped results. A skipped call is
never validated, governed, approved, ledgered or sent. If the stop predicate is already true, the
remaining calls are neither governed nor marked skipped (ADR-0012 rule 6): the run ends.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from services.demo_target_service.app import create_app as create_demo_target
from tests.chaos.harness import (
    TARGET,
    WS,
    Env,
    crash_after_commit,
    is_crash,
    make_env,
    reset_target,
    restarts,
)
from workloads.incident_remediation import client as demo_client

from tether.core.ids import WorkspaceId
from tether.providers.history import ModelTurn, NeutralMessage
from tether.queue import backoff
from tether.runtime.driver import WorkerRuntime
from tether.runtime.graph import REPLAY_DIVERGENCE_REASON
from tether.storage.repos.audit import AuditRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork
from tether.testing.scripted import ScriptedProvider

SCENARIOS = {
    "invalid_then_one": (
        "name: invalid_then_one\nturns:\n"
        "  - tool_calls:\n"
        "      - {tool: no_such_tool, arguments: {}}\n"
        "      - {tool: probe_write, arguments: {target: a}}\n"
        "  - final: done\n"
    ),
    "invalid_then_two": (
        "name: invalid_then_two\nturns:\n"
        "  - tool_calls:\n"
        "      - {tool: no_such_tool, arguments: {}}\n"
        "      - {tool: probe_write, arguments: {target: a}}\n"
        "      - {tool: get_service_health, arguments: {service: checkout}}\n"
        "  - final: done\n"
    ),
    "invalid_then_next_turn": (
        "name: invalid_then_next_turn\nturns:\n"
        "  - tool_calls:\n"
        "      - {tool: no_such_tool, arguments: {}}\n"
        "      - {tool: probe_write, arguments: {target: a}}\n"
        "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
        "  - final: done\n"
    ),
    "approval_then_two": (
        "name: approval_then_two\nturns:\n"
        "  - tool_calls:\n"
        "      - {tool: restart_service, arguments: {service: checkout}}\n"
        "      - {tool: probe_write, arguments: {target: a}}\n"
        "      - {tool: get_service_health, arguments: {service: checkout}}\n"
        "  - final: done\n"
    ),
    "two_allowed": (
        "name: two_allowed\nturns:\n"
        "  - tool_calls:\n"
        "      - {tool: get_service_health, arguments: {service: checkout}}\n"
        "      - {tool: probe_write, arguments: {target: a}}\n"
        "  - final: done\n"
    ),
}


class RecordingProvider(ScriptedProvider):
    """The scripted provider, keeping the history of every model call it answered."""

    def __init__(self, scenarios: Path) -> None:
        super().__init__(scenarios, env="test")
        self.calls: list[list[NeutralMessage]] = []

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        self.calls.append(list(history))
        return await super().complete(history, manifest)


@pytest.fixture(autouse=True)
def demo_target(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    reset_target()
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
    yield
    demo_client.set_transport(None)


@pytest_asyncio.fixture
async def env(seeded: Any, tmp_path: Path) -> AsyncIterator[Env]:
    environment = make_env(seeded.db, tmp_path)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    yield environment


@pytest_asyncio.fixture
async def provider(env: Env) -> RecordingProvider:
    return RecordingProvider(env.tmp_path)


@pytest_asyncio.fixture
async def worker(env: Env, provider: RecordingProvider) -> AsyncIterator[WorkerRuntime]:
    runtime = await env.worker(provider)
    try:
        yield runtime
    finally:
        await runtime.close()


def skips(env: Env, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    """(dedupe_key, tool, payload) of every CALL_SKIPPED event, in chain order."""
    return env.sql(
        "SELECT dedupe_key, tool, payload_masked FROM tether.audit_events"
        " WHERE run_id = %s AND event_type = 'CALL_SKIPPED' ORDER BY seq",
        run_id,
    )


def skip_payload(ordinal: int, trigger: int, outcome: str) -> dict[str, Any]:
    return {
        "step_ordinal": ordinal,
        "call_id": f"call_0_{ordinal}",
        "reason": "earlier_call_not_applied",
        "trigger_step_ordinal": trigger,
        "trigger_call_id": f"call_0_{trigger}",
        "trigger_outcome": outcome,
    }


def counts(env: Env, run_id: uuid.UUID) -> tuple[Any, ...]:
    """(approval rows, ledger rows) of the run."""
    return env.sql(
        "SELECT (SELECT count(*) FROM tether.approvals WHERE run_id = %s),"
        " (SELECT count(*) FROM tether.ledger_entries WHERE run_id = %s)",
        run_id,
        run_id,
    )[0]


def model_results(provider: RecordingProvider, turn: int) -> list[tuple[Any, ...]]:
    """(call_id, outcome) of the tool results the model saw at ``turn``."""
    return [(m.call_id, m.outcome) for m in provider.calls[turn] if m.role == "tool"]


def skipped_contents(provider: RecordingProvider, turn: int) -> list[tuple[Any, ...]]:
    """(call_id, parsed content) of the ``skipped`` results the model saw at ``turn``."""
    return [
        (m.call_id, json.loads(m.content))
        for m in provider.calls[turn]
        if m.role == "tool" and m.outcome == "skipped"
    ]


def skip_content(trigger_call_id: str, outcome: str) -> dict[str, Any]:
    """ADR-0012 rule 4: a skipped result references call k's call ID and outcome."""
    return {
        "reason": "earlier_call_not_applied",
        "trigger_call_id": trigger_call_id,
        "trigger_outcome": outcome,
    }


# Argument values and names of the scripted calls: none may reach a skipped result.
ARGUMENT_TEXT = ("checkout", '"a"', "target", "service")


def assert_no_arguments(provider: RecordingProvider, turn: int) -> None:
    for message in provider.calls[turn]:
        if message.role == "tool" and message.outcome == "skipped":
            for text in ARGUMENT_TEXT:
                assert text not in message.content, (text, message.content)


async def decide(env: Env, approval_id: Any, action: str) -> httpx.Response:
    return await env.call(
        "POST",
        f"/v1/approvals/{approval_id}/{action}",
        json={
            "dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]},
            "reason": "test decision",
        },
    )


async def drain_until_crash(env: Env, worker: WorkerRuntime) -> None:
    try:
        await env.drain(worker)
    except BaseException as error:
        if not is_crash(error):
            raise
    else:
        raise AssertionError("the injected crash did not fire")
    await worker.close()
    env.expire_leases()


# --- A. one remaining call ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_one_call_after_an_invalid_call_is_skipped_and_audited(
    env: Env, worker: WorkerRuntime, provider: RecordingProvider
) -> None:
    run_id = await env.submit("invalid_then_one")
    await env.drain(worker)
    assert env.status(run_id) == "succeeded"
    assert env.events(run_id) == ["RUN_SUBMITTED", "CALL_INVALID", "CALL_SKIPPED", "RUN_FINISHED"]
    assert skips(env, run_id) == [("skipped:1", "probe_write", skip_payload(1, 0, "invalid"))]
    assert counts(env, run_id) == (0, 0)
    assert TARGET.sends == 0
    # Exactly one result per call; the skipped one references call k's call ID and outcome.
    assert model_results(provider, 1) == [("call_0_0", "invalid"), ("call_0_1", "skipped")]
    assert skipped_contents(provider, 1) == [("call_0_1", skip_content("call_0_0", "invalid"))]
    assert_no_arguments(provider, 1)
    await env.assert_reconstructable(run_id)
    inspected = [(e["event_type"], e["tool"]) for e in (await env.view(run_id))["audit"]]
    assert ("CALL_SKIPPED", "probe_write") in inspected


# --- B. several remaining calls -------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_every_call_after_a_rejected_approval_gets_its_own_skip_event(
    env: Env, worker: WorkerRuntime, provider: RecordingProvider
) -> None:
    run_id = await env.submit("approval_then_two")
    await env.drain(worker)
    assert env.status(run_id) == "awaiting_approval"
    assert skips(env, run_id) == []  # require_approval pauses; it never skips (rule 3)
    assert (await decide(env, env.approval_id(run_id), "reject")).status_code == 200
    await env.drain(worker)
    assert env.status(run_id) == "succeeded"
    assert skips(env, run_id) == [
        ("skipped:1", "probe_write", skip_payload(1, 0, "rejected")),
        ("skipped:2", "get_service_health", skip_payload(2, 0, "rejected")),
    ]
    assert counts(env, run_id) == (1, 0)  # only call A's approval; no ledger row at all
    assert TARGET.sends == 0
    assert await restarts() == 0
    assert model_results(provider, 1) == [
        ("call_0_0", "rejected"),
        ("call_0_1", "skipped"),
        ("call_0_2", "skipped"),
    ]
    assert skipped_contents(provider, 1) == [
        ("call_0_1", skip_content("call_0_0", "rejected")),
        ("call_0_2", skip_content("call_0_0", "rejected")),
    ]
    assert_no_arguments(provider, 1)
    # D. ordering: the triggering outcome, then the skips, then what consumes them.
    events = env.events(run_id)
    first_skip = events.index("CALL_SKIPPED")
    assert events.index("APPROVAL_DECIDED") < first_skip
    assert events[first_skip : first_skip + 2] == ["CALL_SKIPPED", "CALL_SKIPPED"]
    assert first_skip + 1 < events.index("RUN_FINISHED")  # the last skip precedes the end
    await env.assert_reconstructable(run_id)


@pytest.mark.asyncio
async def test_d_skip_events_are_recorded_before_the_next_model_turn(
    env: Env, worker: WorkerRuntime
) -> None:
    run_id = await env.submit("invalid_then_next_turn")
    await env.drain(worker)
    assert env.status(run_id) == "succeeded"
    assert env.events(run_id) == [
        "RUN_SUBMITTED",
        "CALL_INVALID",
        "CALL_SKIPPED",
        "POLICY_DECISION",
        "EXECUTION_AUTHORIZED",
        "DISPATCHED",
        "TOOL_OUTCOME",
        "RUN_FINISHED",
    ]
    [(skipped_at, next_turn_recorded)] = env.sql(
        "SELECT (SELECT created_at FROM tether.audit_events"
        "        WHERE run_id = %s AND event_type = 'CALL_SKIPPED'),"
        " (SELECT started_at FROM tether.model_calls WHERE run_id = %s AND turn_ordinal = 1)",
        run_id,
        run_id,
    )
    assert skipped_at < next_turn_recorded
    # The re-proposal in the next turn is a new ordinal: governed and ledgered afresh.
    assert [(o, t) for o, t, *_ in env.ledger(run_id)] == [(2, "get_service_health")]
    await env.assert_reconstructable(run_id)


# --- C. replay ------------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["CALL_INVALID", "CALL_SKIPPED"])
@pytest.mark.asyncio
async def test_c_a_crash_around_the_skip_transaction_never_duplicates_skip_events(
    env: Env, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    crash_after_commit(monkeypatch, event)
    run_id = await env.submit("invalid_then_two")
    await drain_until_crash(env, await env.worker())
    replayed = RecordingProvider(env.tmp_path)
    fresh = await env.worker(replayed)
    try:
        await env.drain(fresh)
        assert env.status(run_id) == "succeeded"
        assert env.events(run_id) == [
            "RUN_SUBMITTED",
            "CALL_INVALID",
            "CALL_SKIPPED",
            "CALL_SKIPPED",
            "RUN_FINISHED",
        ]
        # The replayed node gives the model the same skipped results as an uninterrupted run.
        [second_turn] = replayed.calls
        assert [
            (m.call_id, m.outcome, json.loads(m.content) if m.outcome == "skipped" else None)
            for m in second_turn
            if m.role == "tool"
        ] == [
            ("call_0_0", "invalid", None),
            ("call_0_1", "skipped", skip_content("call_0_0", "invalid")),
            ("call_0_2", "skipped", skip_content("call_0_0", "invalid")),
        ]
        assert skips(env, run_id) == [
            ("skipped:1", "probe_write", skip_payload(1, 0, "invalid")),
            ("skipped:2", "get_service_health", skip_payload(2, 0, "invalid")),
        ]
        assert counts(env, run_id) == (0, 0)
        assert TARGET.sends == 0
        assert env.model_calls(run_id) == 2
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, fresh)
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_c_a_persisted_skip_event_for_another_call_fails_closed(
    env: Env, worker: WorkerRuntime
) -> None:
    """Replay identity (W2.2 semantics): a skip event recorded for a different tool or cause at
    this ordinal is never adopted or rewritten; the run fails closed."""
    run_id = await env.submit("invalid_then_one")
    async with UnitOfWork(worker.engine) as uow:
        await uow.repository(AuditRepository, WorkspaceScope(WorkspaceId(WS))).append(
            worker.keys,
            run_id=run_id,
            dedupe_key="skipped:1",
            event_type="CALL_SKIPPED",
            payload=skip_payload(1, 0, "denied"),
            tool="probe_nonidem",
        )
    await env.drain(worker)
    assert env.sql("SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id) == [
        ("failed", REPLAY_DIVERGENCE_REASON)
    ]
    assert skips(env, run_id) == [("skipped:1", "probe_nonidem", skip_payload(1, 0, "denied"))]
    [(path, ordinal)] = env.sql(
        "SELECT payload_masked->>'path', (payload_masked->>'step_ordinal')::int"
        " FROM tether.audit_events WHERE run_id = %s AND event_type = 'REPLAY_DIVERGENCE'",
        run_id,
    )
    assert (path, ordinal) == ("skip", 1)
    assert counts(env, run_id) == (0, 0)
    assert TARGET.sends == 0
    await env.assert_terminal_stable(run_id, worker)


# --- E. stops ---------------------------------------------------------------------------------


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_e_a_stop_ends_the_run_without_marking_the_remaining_calls_skipped(
    env: Env, worker: WorkerRuntime, stop: str
) -> None:
    """ADR-0012 rule 6: with the stop predicate true, remaining calls are not marked skipped."""
    run_id = await env.submit("approval_then_two")
    await env.drain(worker)
    assert env.status(run_id) == "awaiting_approval"
    if stop == "cancel":
        assert (await env.cancel(run_id)).status_code == 202
    else:
        env.sql(
            "UPDATE tether.runs SET deadline_at = clock_timestamp() WHERE id = %s RETURNING id",
            run_id,
        )
        env.sql(
            "UPDATE tether.jobs SET run_after = clock_timestamp()"
            " WHERE run_id = %s AND kind = 'run_deadline' RETURNING id",
            run_id,
        )
    await env.drain(worker)
    assert env.status(run_id) == ("cancelled" if stop == "cancel" else "timed_out")
    assert skips(env, run_id) == []
    assert counts(env, run_id) == (1, 0)
    assert env.sql("SELECT status FROM tether.approvals WHERE run_id = %s", run_id) == [("closed",)]
    assert TARGET.sends == 0
    assert env.model_calls(run_id) == 1
    await env.assert_reconstructable(run_id)
    await env.assert_terminal_stable(run_id, worker)


@pytest.mark.asyncio
async def test_e_a_cancel_after_the_skips_committed_keeps_them_once_and_authorizes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_after_commit(monkeypatch, "CALL_SKIPPED")
    run_id = await env.submit("invalid_then_two")
    await drain_until_crash(env, await env.worker())
    assert (await env.cancel(run_id)).status_code == 202
    fresh = await env.worker()
    try:
        await env.drain(fresh)
        assert env.status(run_id) == "cancelled"
        assert [d for d, _, _ in skips(env, run_id)] == ["skipped:1", "skipped:2"]
        assert counts(env, run_id) == (0, 0)
        assert "EXECUTION_AUTHORIZED" not in env.events(run_id)
        assert TARGET.sends == 0
        assert env.model_calls(run_id) == 1  # no model call after the stop
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, fresh)
    finally:
        await fresh.close()


# --- F. the normal multi-call path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_f_calls_after_an_applied_call_are_governed_normally_and_not_skipped(
    env: Env, worker: WorkerRuntime, provider: RecordingProvider
) -> None:
    run_id = await env.submit("two_allowed")
    await env.drain(worker)
    assert env.status(run_id) == "succeeded"
    assert skips(env, run_id) == []
    assert [(o, t, s) for o, t, s, _, _ in env.ledger(run_id)] == [
        (0, "get_service_health", "completed"),
        (1, "probe_write", "completed"),
    ]
    assert TARGET.sends == 1
    assert model_results(provider, 1) == [("call_0_0", "applied"), ("call_0_1", "applied")]
    await env.assert_reconstructable(run_id)
