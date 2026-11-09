"""T-26 / T-15: cancellation (TX9), run deadline (TX10) and approval expiry (TX-EA).

ADR-0006: the API and timers only record stop requests; the run driver alone writes the status.
Once a stop is visible no model call, proposal, approval, ledger entry or send begins. A stop never
recalls a dispatched write and never hides an unknown outcome: the run waits for it, and
reconciliation then only records the outcome and terminates the run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from pydantic import BaseModel, SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.core.statuses import JobKind
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.queue import backoff
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS = "ws-c"
OTHER_WS = "ws-c2"
POLICY = {
    "rules": [
        {"tool": "get_service_health", "decision": "allow", "requester_roles": ["operator"]},
        {
            "tool": "restart_service",
            "decision": "require_approval",
            "requester_roles": ["operator"],
            "approver_roles": ["oncall"],
        },
        {"tool": "probe_idem", "decision": "allow", "requester_roles": ["operator"]},
        {"tool": "probe_nonidem", "decision": "allow", "requester_roles": ["operator"]},
    ],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
}
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {"id": ws, "agents": [{"id": "agent-c", "workload": WORKLOAD.name}], "policy": POLICY}
            for ws in (WS, OTHER_WS)
        ]
    }
)


def _scenario(name: str, tool: str, arguments: str) -> str:
    return (
        f"name: {name}\nturns:\n"
        f"  - tool_calls: [{{tool: {tool}, arguments: {arguments}}}]\n"
        f"  - final: {name} done\n"
    )


SCENARIOS = {
    "read": _scenario("read", "get_service_health", "{service: checkout}"),
    "restart": _scenario("restart", "restart_service", "{service: checkout}"),
    "idem": _scenario("idem", "probe_idem", "{target: a}"),
    "nonidem": _scenario("nonidem", "probe_nonidem", "{target: a}"),
    "answer": "name: answer\nturns:\n  - final: answered\n",
}

Hook = Callable[[], Awaitable[None]]


# --- test-only probe tools: count sends; a hook runs while the send is in flight -------------


class ProbeIn(BaseModel):
    target: str


class ProbeOut(BaseModel):
    target: str
    applied: bool


@dataclass
class Target:
    """``script``: ``drop`` = applied, then the connection drops (unknown); ``ok`` = answered."""

    script: list[str] = field(default_factory=list)
    sends: int = 0
    during_send: Hook | None = None

    async def send(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        self.sends += 1
        if self.during_send is not None:
            hook, self.during_send = self.during_send, None
            await hook()
        if (self.script.pop(0) if self.script else "ok") == "drop":
            raise httpx.RemoteProtocolError("connection dropped after the request was sent")
        return ProbeOut(target=args.target, applied=True)

    def reset(self) -> None:
        self.script.clear()
        self.sends = 0
        self.during_send = None


TARGET = Target()


def _probe(name: str, idempotency: Any) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version="1.0.0",
        description=f"Test-only {name}.",
        input_model=ProbeIn,
        output_model=ProbeOut,
        effect="write",
        idempotency=idempotency,
        handler=TARGET.send,
    )


PROBES = (_probe("probe_idem", "supported"), _probe("probe_nonidem", "unsupported"))
TEST_WORKLOAD = Workload(
    name=WORKLOAD.name,
    tools=(*WORKLOAD.tools, *PROBES),
    agent=AgentDefinition(
        system_prompt="test", tools=(*WORKLOAD.agent.tools, *(p.name for p in PROBES))
    ),
)


class HookedProvider:
    """The scripted provider; ``during_call`` runs while a model request is in flight."""

    def __init__(self, inner: ScriptedProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.during_call: Hook | None = None

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        if self.during_call is not None:
            hook, self.during_call = self.during_call, None
            await hook()
        return await self.inner.complete(history, manifest)


# --- fixtures and helpers -------------------------------------------------------------------


def _settings(db: Any, tmp_path: Path, env: str = "test") -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env=env,
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def fake_targets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    TARGET.reset()
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
    yield
    demo_client.set_transport(None)


@pytest_asyncio.fixture
async def runtime(seeded: Any, tmp_path: Path) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    provider = HookedProvider(ScriptedProvider(tmp_path, env="test"))
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path), bootstrap=BOOT, provider=provider
    )
    runtime.registries[WORKLOAD.name] = ToolRegistry(TEST_WORKLOAD)
    try:
        yield runtime
    finally:
        await runtime.close()


def provider(runtime: WorkerRuntime) -> HookedProvider:
    assert isinstance(runtime.provider, HookedProvider)
    return runtime.provider


async def call_api(
    seeded: Any, tmp_path: Path, method: str, url: str, env: str = "test", **kwargs: Any
) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path, env=env))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def submit(seeded: Any, tmp_path: Path, scenario: str) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": "agent-c",
            "requester": {"sub": "alice", "roles": ["operator"]},
        },
        "task_input": {"message": f"go #scenario={scenario}"},
    }
    response = await call_api(seeded, tmp_path, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def cancel(
    seeded: Any,
    tmp_path: Path,
    run_id: uuid.UUID,
    *,
    sub: str = "alice",
    workspace: str = WS,
    env: str = "test",
) -> httpx.Response:
    body = {"dev_identity": {"workspace_id": workspace, "sub": sub, "roles": []}}
    return await call_api(
        seeded, tmp_path, "POST", f"/v1/tasks/runs/{run_id}/cancel", env=env, json=body
    )


async def decide(
    seeded: Any, tmp_path: Path, approval_id: Any, action: str = "approve"
) -> httpx.Response:
    body = {
        "dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver", "oncall"]},
        "reason": "looks right",
    }
    return await call_api(
        seeded, tmp_path, "POST", f"/v1/approvals/{approval_id}/{action}", json=body
    )


async def reconcile(seeded: Any, tmp_path: Path, ledger_id: Any, outcome: str) -> httpx.Response:
    body = {
        "dev_identity": {"workspace_id": WS, "sub": "dave", "roles": ["reconciler"]},
        "outcome": outcome,
        "justification": "checked the target by hand",
    }
    return await call_api(
        seeded, tmp_path, "POST", f"/v1/ops/ledger/{ledger_id}/reconcile", json=body
    )


async def drain(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> None:
    for _ in range(60):
        if not await process_next_job(runtime, kinds):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_row(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    """(status, outcome_summary, cancel_requested_by, deadline exceeded recorded)."""
    return rows(
        seeded,
        "SELECT status, outcome_summary, cancel_requested_by, deadline_exceeded_at IS NOT NULL"
        " FROM tether.runs WHERE id = %s",
        run_id,
    )[0]


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event_type for (event_type,) in found]


def count(seeded: Any, table: str, run_id: uuid.UUID) -> int:
    return rows(seeded, f"SELECT count(*) FROM tether.{table} WHERE run_id = %s", run_id)[0][0]  # noqa: S608


def approval(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    [found] = rows(seeded, "SELECT id, status FROM tether.approvals WHERE run_id = %s", run_id)
    return found


def entry(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    """(id, state, attempts, outcome_reason) of the run's only ledger entry."""
    [found] = rows(
        seeded,
        "SELECT id, state, attempts, outcome_reason FROM tether.ledger_entries WHERE run_id = %s",
        run_id,
    )
    return found


def wakes(seeded: Any, run_id: uuid.UUID) -> int:
    return rows(
        seeded,
        "SELECT count(*) FROM tether.jobs WHERE run_id = %s AND kind = 'advance_run'",
        run_id,
    )[0][0]


def pass_deadline(seeded: Any, run_id: uuid.UUID) -> None:
    """The run's maximum duration is over now, and its timer job is due."""
    rows(
        seeded,
        "UPDATE tether.runs SET deadline_at = clock_timestamp() WHERE id = %s RETURNING id",
        run_id,
    )
    rows(
        seeded,
        "UPDATE tether.jobs SET run_after = clock_timestamp()"
        " WHERE run_id = %s AND kind = 'run_deadline' RETURNING id",
        run_id,
    )


def lapse_approval(seeded: Any, run_id: uuid.UUID, *, timer_due: bool) -> None:
    rows(
        seeded,
        "UPDATE tether.approvals SET expires_at = clock_timestamp() WHERE run_id = %s RETURNING id",
        run_id,
    )
    if timer_due:
        rows(
            seeded,
            "UPDATE tether.jobs SET run_after = clock_timestamp()"
            " WHERE run_id = %s AND kind = 'expire_approval' RETURNING id",
            run_id,
        )


def duplicate_job(seeded: Any, run_id: uuid.UUID, kind: str) -> None:
    rows(
        seeded,
        "INSERT INTO tether.jobs (id, kind, workspace_id, run_id, ref_id, max_attempts)"
        " SELECT %s, kind, workspace_id, run_id, ref_id, max_attempts FROM tether.jobs"
        " WHERE run_id = %s AND kind = %s LIMIT 1 RETURNING id",
        uuid.uuid4(),
        run_id,
        kind,
    )


async def chain_valid(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> bool:
    response = await call_api(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    assert response.status_code == 200, response.text
    return bool(response.json()["audit_chain_valid"])


async def stop(kind: str, seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> None:
    if kind == "cancel":
        response = await cancel(seeded, tmp_path, run_id)
        assert response.status_code == 202, response.text
    else:
        pass_deadline(seeded, run_id)


STOPPED = {
    "cancel": ("cancelled", "cancelled by alice"),
    "deadline": ("timed_out", "deadline passed"),
}


# --- the timers are enqueued with the rows they guard -----------------------------------------


@pytest.mark.asyncio
async def test_tx1_and_tx3_enqueue_the_deadline_and_expiry_timers(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "restart")
    [(due_after_deadline,)] = rows(
        seeded,
        "SELECT j.run_after >= r.deadline_at FROM tether.jobs j JOIN tether.runs r"
        " ON r.id = j.run_id WHERE j.run_id = %s AND j.kind = 'run_deadline'",
        run_id,
    )
    assert due_after_deadline
    await drain(runtime)
    approval_id, status = approval(seeded, run_id)
    assert status == "pending"
    [(ref_id, due_after_expiry)] = rows(
        seeded,
        "SELECT j.ref_id, j.run_after >= a.expires_at FROM tether.jobs j JOIN tether.approvals a"
        " ON a.id = j.ref_id WHERE j.run_id = %s AND j.kind = 'expire_approval'",
        run_id,
    )
    assert (ref_id, due_after_expiry) == (approval_id, True)


# --- cancellation ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t26_a_run_cancelled_before_it_starts_makes_no_model_call(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "read")
    response = await cancel(seeded, tmp_path, run_id)
    assert response.status_code == 202, response.text
    # The API records the request only; the run driver writes the status (ADR-0006).
    assert response.json()["status"] == "queued"
    assert run_row(seeded, run_id) == ("queued", None, "alice", False)
    await drain(runtime)
    assert run_row(seeded, run_id) == ("cancelled", "cancelled by alice", "alice", False)
    assert count(seeded, "model_calls", run_id) == 0
    assert events(seeded, run_id) == ["RUN_SUBMITTED", "CANCEL_REQUESTED", "RUN_FINISHED"]
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.parametrize("kind", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_t26_a_stop_during_a_model_call_ends_the_run_before_any_proposal(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, kind: str
) -> None:
    run_id = await submit(seeded, tmp_path, "read")

    async def stop_now() -> None:
        await stop(kind, seeded, tmp_path, run_id)

    provider(runtime).during_call = stop_now
    await drain(runtime)
    status, summary, *_ = run_row(seeded, run_id)
    assert (status, summary) == STOPPED[kind]
    # The proposal received after the stop is never governed: no decision, no ledger entry.
    assert count(seeded, "model_calls", run_id) == 1
    assert "POLICY_DECISION" not in events(seeded, run_id)
    assert count(seeded, "ledger_entries", run_id) == 0
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.parametrize("kind", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_t26_a_final_answer_received_after_a_stop_does_not_end_the_run_as_succeeded(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, kind: str
) -> None:
    """ADR-0006: the stop predicate is checked again as soon as the model response returns."""
    run_id = await submit(seeded, tmp_path, "answer")

    async def stop_now() -> None:
        await stop(kind, seeded, tmp_path, run_id)

    provider(runtime).during_call = stop_now
    await drain(runtime)
    status, summary, *_ = run_row(seeded, run_id)
    assert (status, summary) == STOPPED[kind]
    assert count(seeded, "model_calls", run_id) == 1


@pytest.mark.parametrize("kind", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_t26_a_stop_while_awaiting_approval_closes_it_and_a_later_approve_is_refused(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, kind: str
) -> None:
    run_id = await submit(seeded, tmp_path, "restart")
    await drain(runtime)
    assert run_row(seeded, run_id)[0] == "awaiting_approval"
    approval_id, _ = approval(seeded, run_id)
    await stop(kind, seeded, tmp_path, run_id)
    await drain(runtime)
    assert approval(seeded, run_id) == (approval_id, "closed")
    status, summary, _, deadline_recorded = run_row(seeded, run_id)
    assert (status, summary) == STOPPED[kind]
    assert deadline_recorded is (kind == "deadline")
    expected = "CANCEL_REQUESTED" if kind == "cancel" else "DEADLINE_EXCEEDED"
    assert expected in events(seeded, run_id)
    assert events(seeded, run_id).count("APPROVAL_CLOSED") == 1
    assert count(seeded, "model_calls", run_id) == 1
    assert count(seeded, "ledger_entries", run_id) == 0
    late = await decide(seeded, tmp_path, approval_id)
    assert late.status_code == 409, late.text
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t26_a_cancel_before_dispatch_means_the_write_is_never_sent(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "nonidem")
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    assert entry(seeded, run_id)[1] == "pending"
    await cancel(seeded, tmp_path, run_id)
    await drain(runtime)
    _, state, attempts, reason = entry(seeded, run_id)
    assert (state, attempts, reason) == ("failed", 0, "not applied: run stopped before dispatch")
    assert TARGET.sends == 0
    assert run_row(seeded, run_id)[:2] == ("cancelled", "cancelled by alice")
    assert count(seeded, "model_calls", run_id) == 1


@pytest.mark.parametrize("kind", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_t26_a_dispatched_write_is_not_recalled_and_its_outcome_ends_the_run(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, kind: str
) -> None:
    run_id = await submit(seeded, tmp_path, "nonidem")
    seen: list[str] = []

    async def stop_mid_send() -> None:
        await stop(kind, seeded, tmp_path, run_id)
        seen.append(run_row(seeded, run_id)[0])

    TARGET.during_send = stop_mid_send
    await drain(runtime)
    # While the write was in flight the run showed awaiting_tool plus the stop flag.
    assert seen == ["awaiting_tool"]
    assert entry(seeded, run_id)[1] == "completed"
    assert TARGET.sends == 1
    status, summary, *_ = run_row(seeded, run_id)
    assert (status, summary) == STOPPED[kind]
    assert count(seeded, "model_calls", run_id) == 1
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [("cancel", "applied"), ("cancel", "not_applied"), ("deadline", "applied")],
)
@pytest.mark.asyncio
async def test_t26_an_unknown_write_after_a_stop_goes_to_reconciliation_then_terminates(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, kind: str, outcome: str
) -> None:
    run_id = await submit(seeded, tmp_path, "nonidem")

    async def stop_mid_send() -> None:
        await stop(kind, seeded, tmp_path, run_id)

    TARGET.during_send = stop_mid_send
    TARGET.script[:] = ["drop"]
    await drain(runtime)
    ledger_id, state, *_ = entry(seeded, run_id)
    assert state == "indeterminate"
    # The unknown outcome is not hidden behind a clean cancellation or timeout.
    assert run_row(seeded, run_id)[0] == "awaiting_reconciliation"
    response = await reconcile(seeded, tmp_path, ledger_id, outcome)
    assert response.status_code == 200, response.text
    await drain(runtime)
    status, summary, *_ = run_row(seeded, run_id)
    expected_status, expected_summary = STOPPED[kind]
    assert status == expected_status
    assert summary == f"{expected_summary}; probe_nonidem reconciled_{outcome}"
    # Reconciling a stopped run records the outcome and terminates it: no further model call.
    assert count(seeded, "model_calls", run_id) == 1
    assert TARGET.sends == 1
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t26_an_idempotent_write_is_not_re_sent_after_a_cancel(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "idem")

    async def cancel_mid_send() -> None:
        await cancel(seeded, tmp_path, run_id)

    TARGET.during_send = cancel_mid_send
    TARGET.script[:] = ["drop", "ok"]
    await drain(runtime)
    _, state, attempts, _ = entry(seeded, run_id)
    assert (state, attempts, TARGET.sends) == ("indeterminate", 1, 1)
    assert "REDISPATCHED" not in events(seeded, run_id)
    assert run_row(seeded, run_id)[0] == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_t26_cancel_is_requester_only_workspace_scoped_and_idempotent(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "restart")
    await drain(runtime)
    assert (await cancel(seeded, tmp_path, run_id, sub="carol")).status_code == 404
    other = await cancel(seeded, tmp_path, run_id, workspace=OTHER_WS)
    assert other.status_code == 404
    assert (await cancel(seeded, tmp_path, uuid.uuid4())).status_code == 404
    assert (await cancel(seeded, tmp_path, run_id, env="prod")).status_code == 401
    assert run_row(seeded, run_id)[2] is None
    wakes_before = wakes(seeded, run_id)
    first = await cancel(seeded, tmp_path, run_id)
    again = await cancel(seeded, tmp_path, run_id)
    assert (first.status_code, again.status_code) == (202, 202)
    assert first.json()["cancel_requested_at"] == again.json()["cancel_requested_at"]
    assert events(seeded, run_id).count("CANCEL_REQUESTED") == 1
    assert events(seeded, run_id).count("APPROVAL_CLOSED") == 1
    assert wakes(seeded, run_id) == wakes_before + 1
    await drain(runtime)
    assert run_row(seeded, run_id)[0] == "cancelled"
    # Cancelling a terminal run is a no-op.
    finished = len(events(seeded, run_id))
    late = await cancel(seeded, tmp_path, run_id)
    assert (late.status_code, late.json()["status"]) == (202, "cancelled")
    assert len(events(seeded, run_id)) == finished
    assert await chain_valid(seeded, tmp_path, run_id)


# --- deadline timer (TX10) ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_deadline_timer_records_the_deadline_once(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "restart")
    await drain(runtime)
    pass_deadline(seeded, run_id)
    duplicate_job(seeded, run_id, "run_deadline")
    await drain(runtime)
    assert run_row(seeded, run_id) == ("timed_out", "deadline passed", None, True)
    assert events(seeded, run_id).count("DEADLINE_EXCEEDED") == 1
    assert events(seeded, run_id).count("APPROVAL_CLOSED") == 1
    assert rows(
        seeded,
        "SELECT state FROM tether.jobs WHERE run_id = %s AND kind = 'run_deadline'",
        run_id,
    ) == [("done",), ("done",)]
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_an_exhausted_timer_job_is_dead_lettered_with_a_wake(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "read")
    await drain(runtime)
    rows(
        seeded,
        "UPDATE tether.jobs SET attempts = max_attempts, run_after = clock_timestamp()"
        " WHERE run_id = %s AND kind = 'run_deadline' RETURNING id",
        run_id,
    )
    wakes_before = wakes(seeded, run_id)
    assert await process_next_job(runtime, kinds=(JobKind.RUN_DEADLINE,))
    assert rows(
        seeded,
        "SELECT state, attempts FROM tether.jobs WHERE run_id = %s AND kind = 'run_deadline'",
        run_id,
    ) == [("dead", 11)]
    assert wakes(seeded, run_id) == wakes_before + 1


# --- approval expiry (TX-EA) ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_t15_an_expired_approval_is_expired_once_and_the_run_moves_on(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "restart")
    await drain(runtime)
    approval_id, _ = approval(seeded, run_id)
    lapse_approval(seeded, run_id, timer_due=True)
    duplicate_job(seeded, run_id, "expire_approval")
    await drain(runtime)
    assert approval(seeded, run_id) == (approval_id, "expired")
    assert events(seeded, run_id).count("APPROVAL_EXPIRED") == 1
    # The model is told the approval expired and the run continues; nothing was executed.
    assert run_row(seeded, run_id)[:2] == ("succeeded", "restart done")
    assert count(seeded, "model_calls", run_id) == 2
    assert count(seeded, "ledger_entries", run_id) == 0
    late = await decide(seeded, tmp_path, approval_id)
    assert late.status_code == 409, late.text
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t15_an_approval_past_its_expiry_is_refused_before_the_timer_runs(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "restart")
    await drain(runtime)
    approval_id, _ = approval(seeded, run_id)
    lapse_approval(seeded, run_id, timer_due=False)
    late = await decide(seeded, tmp_path, approval_id)
    assert late.status_code == 409, late.text
    assert approval(seeded, run_id) == (approval_id, "pending")
    assert count(seeded, "ledger_entries", run_id) == 0
