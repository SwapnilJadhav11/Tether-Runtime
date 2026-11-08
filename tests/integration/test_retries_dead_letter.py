"""Bounded retries and TX12 dead-letter (§11 AD-08, §12.3, §12.4, ADR-0004, ADR-0007, ADR-0008).

Reads and idempotent writes with an unknown outcome are re-sent with the same key, through the
stop-gated TX6r record, after a PD-13 backoff carried by the job's ``run_after``; the ledger's send
budget (PD-14, descriptor default 3) bounds them. Non-idempotent writes are never re-sent. A job
leased past ``max_attempts`` is marked ``dead`` in the same transaction as its TX12 domain outcome
and a wake.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
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
from tether.queue import postgres as queue
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.handlers import advance_run, execute_tool
from tether.worker.main import process_next_job

WS = "ws-q"
POLICY = {
    "rules": [
        {"tool": name, "decision": "allow", "requester_roles": ["operator"]}
        for name in ("probe_read", "probe_idem", "probe_nonidem")
    ],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
}
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": "agent-q", "workload": "incident_remediation"}],
                "policy": POLICY,
            }
        ]
    }
)
SCENARIOS = {
    name: (
        f"name: {name}\nturns:\n"
        f"  - tool_calls: [{{tool: probe_{name}, arguments: {{target: a}}}}]\n"
        f"  - final: {name} done\n"
    )
    for name in ("read", "idem", "nonidem")
}


# --- a fake target: counts sends and the effects it actually applied --------------------------


class ProbeIn(BaseModel):
    target: str


class ProbeOut(BaseModel):
    target: str
    applied: bool


@dataclass
class Target:
    """``script`` says what each send does: ``fail``/``timeout`` = no response and nothing
    applied; ``drop`` = applied, then the connection drops (unknown); ``ok`` = answered."""

    script: list[str] = field(default_factory=list)
    sends: int = 0
    keys: list[str] = field(default_factory=list)
    # The idempotent target applies a key once (it dedupes); the other one applies every send.
    idem_effects: set[str] = field(default_factory=set)
    nonidem_effects: int = 0

    def _step(self, ctx: ToolContext) -> str:
        self.sends += 1
        self.keys.append(ctx.idempotency_key)
        return self.script.pop(0) if self.script else "ok"

    async def read(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        if self._step(ctx) == "fail":
            raise httpx.ReadTimeout("no response")
        return ProbeOut(target=args.target, applied=False)

    async def idem(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        step = self._step(ctx)
        if step == "timeout":
            raise httpx.ReadTimeout("no response")
        self.idem_effects.add(ctx.idempotency_key)
        if step == "drop":
            raise httpx.RemoteProtocolError("connection dropped after the request was sent")
        return ProbeOut(target=args.target, applied=True)

    async def nonidem(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        step = self._step(ctx)
        self.nonidem_effects += 1
        if step == "drop":
            raise httpx.RemoteProtocolError("connection dropped after the request was sent")
        return ProbeOut(target=args.target, applied=True)

    def reset(self) -> None:
        self.script.clear()
        self.keys.clear()
        self.idem_effects.clear()
        self.sends = self.nonidem_effects = 0


TARGET = Target()


def _tool(name: str, effect: Any, idempotency: Any, handler: Any) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version="1.0.0",
        description=f"Test-only {name}.",
        input_model=ProbeIn,
        output_model=ProbeOut,
        effect=effect,
        idempotency=idempotency,
        handler=handler,
    )


PROBES = (
    _tool("probe_read", "read", None, TARGET.read),
    _tool("probe_idem", "write", "supported", TARGET.idem),
    _tool("probe_nonidem", "write", "unsupported", TARGET.nonidem),
)
TEST_WORKLOAD = Workload(
    name=WORKLOAD.name,
    tools=(*WORKLOAD.tools, *PROBES),
    agent=AgentDefinition(
        system_prompt="test", tools=(*WORKLOAD.agent.tools, *(p.name for p in PROBES))
    ),
)


class RecordingProvider:
    def __init__(self, inner: ScriptedProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.calls: list[list[NeutralMessage]] = []

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        self.calls.append(list(history))
        return await self.inner.complete(history, manifest)


# --- fixtures and helpers -------------------------------------------------------------------


def _settings(db: Any, tmp_path: Path) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def fake_target(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    TARGET.reset()
    # Retries are immediate unless a test sets its own delay.
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
    yield
    demo_client.set_transport(None)


@pytest_asyncio.fixture
async def runtime(seeded: Any, tmp_path: Path) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    provider = RecordingProvider(ScriptedProvider(tmp_path, env="test"))
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path), bootstrap=BOOT, provider=provider
    )
    runtime.registries[WORKLOAD.name] = ToolRegistry(TEST_WORKLOAD)
    try:
        yield runtime
    finally:
        await runtime.close()


async def call_api(
    seeded: Any, tmp_path: Path, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def submit(seeded: Any, tmp_path: Path, scenario: str) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": "agent-q",
            "requester": {"sub": "alice", "roles": ["operator"]},
        },
        "task_input": {"message": f"probe #scenario={scenario}"},
    }
    response = await call_api(seeded, tmp_path, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def drain(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> None:
    for _ in range(60):
        if not await process_next_job(runtime, kinds):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_status(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return rows(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


def generation(seeded: Any, run_id: uuid.UUID) -> int:
    return rows(seeded, "SELECT driver_generation FROM tether.runs WHERE id = %s", run_id)[0][0]


def entry(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    """(id, state, attempts, outcome_reason, idempotency_key) of the run's only ledger entry."""
    [found] = rows(
        seeded,
        "SELECT id, state, attempts, outcome_reason, idempotency_key"
        " FROM tether.ledger_entries WHERE run_id = %s",
        run_id,
    )
    return found


def jobs(seeded: Any, run_id: uuid.UUID, kind: str) -> list[tuple[Any, ...]]:
    return rows(
        seeded,
        "SELECT state, attempts FROM tether.jobs WHERE run_id = %s AND kind = %s"
        " ORDER BY created_at",
        run_id,
        kind,
    )


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event_type for (event_type,) in found]


def tool_results(runtime: WorkerRuntime) -> list[tuple[str | None, Any]]:
    provider = runtime.provider
    assert isinstance(provider, RecordingProvider)
    return [(m.outcome, m.content) for m in provider.calls[-1] if m.role == "tool"]


def exhaust(seeded: Any, run_id: uuid.UUID, kind: str) -> None:
    """Spend every delivery of the run's ``kind`` job: its next lease is past ``max_attempts``."""
    rows(
        seeded,
        "UPDATE tether.jobs SET attempts = max_attempts"
        " WHERE run_id = %s AND kind = %s AND state = 'ready' RETURNING id",
        run_id,
        kind,
    )


def add_exhausted_wake(seeded: Any, run_id: uuid.UUID) -> None:
    rows(
        seeded,
        "INSERT INTO tether.jobs (id, kind, workspace_id, run_id, max_attempts, attempts)"
        " VALUES (%s, 'advance_run', %s, %s, 8, 8) RETURNING id",
        uuid.uuid4(),
        WS,
        run_id,
    )


def expire_leases(seeded: Any, run_id: uuid.UUID) -> None:
    rows(
        seeded,
        "UPDATE tether.jobs SET lease_expires_at = clock_timestamp()"
        " WHERE run_id = %s AND state = 'leased' RETURNING id",
        run_id,
    )


def mark_in_flight(seeded: Any, run_id: uuid.UUID) -> None:
    """A crash after TX6: the entry is ``dispatched`` with no outcome recorded."""
    rows(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'dispatched', attempts = 1"
        " WHERE run_id = %s RETURNING id",
        run_id,
    )


async def chain_valid(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> bool:
    response = await call_api(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    assert response.status_code == 200, response.text
    return bool(response.json()["audit_chain_valid"])


# --- retries: reads and idempotent writes, same key, bounded -----------------------------------


@pytest.mark.asyncio
async def test_t03_a_retryable_read_failure_is_retried_until_it_succeeds(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["fail", "fail", "ok"]
    run_id = await submit(seeded, tmp_path, "read")
    await drain(runtime)
    _, state, attempts, _, key = entry(seeded, run_id)
    assert (state, attempts, TARGET.sends) == ("completed", 3, 3)
    assert TARGET.keys == [key, key, key]
    assert events(seeded, run_id).count("REDISPATCHED") == 2
    # One delivery per send: each retry went back through the queue.
    assert jobs(seeded, run_id, "execute_tool") == [("done", 3)]
    assert tool_results(runtime) == [("applied", '{"applied": false, "target": "a"}')]
    assert run_status(seeded, run_id) == ("succeeded", "read done")
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t04_a_read_failing_every_attempt_stops_at_the_budget_as_a_typed_failure(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["fail"] * 10
    run_id = await submit(seeded, tmp_path, "read")
    await drain(runtime)
    _, state, attempts, reason, _ = entry(seeded, run_id)
    assert (state, attempts, TARGET.sends) == ("failed", 3, 3)
    assert reason.startswith("outcome unknown")
    [(outcome, _)] = tool_results(runtime)
    assert outcome == "failed"
    assert run_status(seeded, run_id) == ("succeeded", "read done")
    await drain(runtime)
    assert TARGET.sends == 3
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_an_idempotent_write_is_retried_with_the_same_key_and_applies_once(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["drop", "ok"]
    run_id = await submit(seeded, tmp_path, "idem")
    await drain(runtime)
    _, state, attempts, _, key = entry(seeded, run_id)
    assert (state, attempts, TARGET.sends) == ("completed", 2, 2)
    assert TARGET.keys == [key, key]
    assert TARGET.idem_effects == {key}
    assert events(seeded, run_id).count("REDISPATCHED") == 1
    assert run_status(seeded, run_id) == ("succeeded", "idem done")
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t20_an_idempotent_write_unknown_on_every_attempt_becomes_indeterminate(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["drop", "timeout", "drop", "ok"]
    run_id = await submit(seeded, tmp_path, "idem")
    await drain(runtime)
    ledger_id, state, attempts, reason, key = entry(seeded, run_id)
    assert (state, attempts, TARGET.sends) == ("indeterminate", 3, 3)
    assert reason.startswith("outcome unknown")
    assert TARGET.keys == [key, key, key]
    assert TARGET.idem_effects == {key}
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    await drain(runtime)
    assert TARGET.sends == 3
    # The reconciliation path is unchanged: a human says it applied; nothing is re-sent.
    body = {
        "dev_identity": {"workspace_id": WS, "sub": "dave", "roles": ["reconciler"]},
        "outcome": "applied",
        "justification": "checked the target",
    }
    response = await call_api(
        seeded, tmp_path, "POST", f"/v1/ops/ledger/{ledger_id}/reconcile", json=body
    )
    assert response.status_code == 200, response.text
    await drain(runtime)
    assert TARGET.sends == 3
    assert run_status(seeded, run_id) == ("succeeded", "idem done")
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t17_an_unknown_non_idempotent_write_is_never_retried(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["drop", "ok"]
    run_id = await submit(seeded, tmp_path, "nonidem")
    await drain(runtime)
    _, state, attempts, _, _ = entry(seeded, run_id)
    assert (state, attempts, TARGET.sends, TARGET.nonidem_effects) == ("indeterminate", 1, 1, 1)
    assert jobs(seeded, run_id, "execute_tool") == [("done", 1)]
    assert "REDISPATCHED" not in events(seeded, run_id)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    await drain(runtime)
    assert TARGET.sends == 1


@pytest.mark.asyncio
async def test_t20_a_redelivery_after_the_last_allowed_send_does_not_send_again(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """A worker died right after send 3 of 3: the budget spans redeliveries (§12.4)."""
    run_id = await submit(seeded, tmp_path, "idem")
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    rows(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'dispatched', attempts = 3"
        " WHERE run_id = %s RETURNING id",
        run_id,
    )
    await drain(runtime)
    _, state, attempts, reason, _ = entry(seeded, run_id)
    assert (state, attempts, TARGET.sends) == ("indeterminate", 3, 0)
    assert reason == "outcome unknown after 3 attempts; not re-sent"
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_a_retry_is_not_delivered_before_its_backoff_delay(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []

    def thirty_seconds(attempt: int, rng: Any = None) -> float:
        asked.append(attempt)
        return 30.0

    monkeypatch.setattr(backoff, "tool_backoff_seconds", thirty_seconds)
    TARGET.script[:] = ["fail", "ok"]
    run_id = await submit(seeded, tmp_path, "read")
    await drain(runtime)
    assert asked == [1]
    [(state, attempts, delay)] = rows(
        seeded,
        "SELECT state, attempts, extract(epoch FROM run_after - clock_timestamp())::float"
        " FROM tether.jobs WHERE run_id = %s AND kind = 'execute_tool'",
        run_id,
    )
    assert (state, attempts) == ("ready", 1)
    assert 25.0 < delay <= 30.0
    assert not await process_next_job(runtime)
    assert TARGET.sends == 1
    rows(
        seeded,
        "UPDATE tether.jobs SET run_after = clock_timestamp()"
        " WHERE run_id = %s AND kind = 'execute_tool' RETURNING id",
        run_id,
    )
    await drain(runtime)
    assert entry(seeded, run_id)[1:3] == ("completed", 2)
    assert run_status(seeded, run_id) == ("succeeded", "read done")


@pytest.mark.asyncio
async def test_a_stop_before_a_retry_means_no_re_send(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0008: no send starts once a stop is visible; the entry becomes indeterminate."""
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 30.0)
    TARGET.script[:] = ["drop", "ok"]
    run_id = await submit(seeded, tmp_path, "idem")
    await drain(runtime)
    rows(
        seeded,
        "UPDATE tether.runs SET deadline_at = clock_timestamp() WHERE id = %s RETURNING id",
        run_id,
    )
    rows(
        seeded,
        "UPDATE tether.jobs SET run_after = clock_timestamp()"
        " WHERE run_id = %s AND kind = 'execute_tool' RETURNING id",
        run_id,
    )
    await drain(runtime)
    assert entry(seeded, run_id)[1:3] == ("indeterminate", 1)
    assert TARGET.sends == 1
    assert "REDISPATCHED" not in events(seeded, run_id)


# --- TX12: exhausted delivery always yields a domain outcome and a wake ---------------------


@pytest.mark.asyncio
async def test_advance_run_delivery_stops_at_max_attempts_and_tx12_fails_the_run(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crashes = 0

    async def crash(*args: Any, **kwargs: Any) -> None:
        nonlocal crashes
        crashes += 1
        raise RuntimeError("driver crashed")

    monkeypatch.setattr(advance_run, "drive", crash)
    run_id = await submit(seeded, tmp_path, "read")
    for _ in range(8):
        with pytest.raises(RuntimeError, match="driver crashed"):
            await process_next_job(runtime)
        expire_leases(seeded, run_id)
    assert crashes == 8
    before = generation(seeded, run_id)
    assert await process_next_job(runtime)  # the ninth lease is past max_attempts: TX12
    assert crashes == 8
    assert jobs(seeded, run_id, "advance_run") == [("dead", 9), ("ready", 0)]
    assert run_status(seeded, run_id) == ("failed", "system_error")
    assert generation(seeded, run_id) == before + 1  # fences any older driver (ADR-0005)
    assert events(seeded, run_id)[-2:] == ["DEAD_LETTERED", "RUN_FINISHED"]
    await drain(runtime)  # the TX12 wake is a no-op on a terminal run
    assert run_status(seeded, run_id) == ("failed", "system_error")
    assert TARGET.sends == 0
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.parametrize(
    ("scenario", "in_flight", "ledger_state", "reason", "run"),
    [
        ("read", False, "failed", "not applied: delivery exhausted before dispatch", "succeeded"),
        ("idem", False, "failed", "not applied: delivery exhausted before dispatch", "succeeded"),
        ("read", True, "failed", "outcome unknown: delivery exhausted", "succeeded"),
        ("idem", True, "indeterminate", "outcome unknown: delivery exhausted", None),
        ("nonidem", True, "indeterminate", "outcome unknown: delivery exhausted", None),
    ],
)
@pytest.mark.asyncio
async def test_tx12_an_exhausted_execute_tool_job_resolves_its_ledger_entry(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    scenario: str,
    in_flight: bool,
    ledger_state: str,
    reason: str,
    run: str | None,
) -> None:
    run_id = await submit(seeded, tmp_path, scenario)
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    if in_flight:
        mark_in_flight(seeded, run_id)
    exhaust(seeded, run_id, "execute_tool")
    wakes_before = len(jobs(seeded, run_id, "advance_run"))
    assert await process_next_job(runtime, kinds=(JobKind.EXECUTE_TOOL,))
    assert jobs(seeded, run_id, "execute_tool") == [("dead", 6)]
    assert entry(seeded, run_id)[1] == ledger_state
    assert entry(seeded, run_id)[3] == reason
    assert TARGET.sends == 0  # exhaustion never sends
    assert events(seeded, run_id)[-1] == "TOOL_OUTCOME"
    assert len(jobs(seeded, run_id, "advance_run")) == wakes_before + 1
    await drain(runtime)
    if run is None:
        assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    else:
        [(outcome, _)] = tool_results(runtime)
        assert outcome == "failed"
        assert run_status(seeded, run_id)[0] == run
    assert TARGET.sends == 0
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_tx12_advance_run_with_a_write_in_flight_holds_the_run_until_it_resolves(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """ADR-0004: an entry in flight → ``awaiting_reconciliation``; PD-35: resume once terminal."""
    run_id = await submit(seeded, tmp_path, "idem")
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    mark_in_flight(seeded, run_id)
    add_exhausted_wake(seeded, run_id)
    assert await process_next_job(runtime, kinds=(JobKind.ADVANCE_RUN,))
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    assert events(seeded, run_id)[-1] == "DEAD_LETTERED"
    assert jobs(seeded, run_id, "advance_run")[-2:] == [("dead", 9), ("ready", 0)]
    held = generation(seeded, run_id)
    # Its wake does not resume the run while the write is still in flight.
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    assert generation(seeded, run_id) == held
    # The write's own job re-sends with the same key; its outcome wakes and resumes the run.
    await drain(runtime)
    _, state, attempts, _, key = entry(seeded, run_id)
    assert (state, attempts, TARGET.sends) == ("completed", 2, 1)
    assert TARGET.idem_effects == {key}
    assert run_status(seeded, run_id) == ("succeeded", "idem done")
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_tx12_with_an_indeterminate_entry_holds_the_run_for_reconciliation(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["drop"]
    run_id = await submit(seeded, tmp_path, "nonidem")
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    add_exhausted_wake(seeded, run_id)
    await drain(runtime)
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    assert "DEAD_LETTERED" in events(seeded, run_id)
    assert "RUN_FINISHED" not in events(seeded, run_id)
    assert TARGET.sends == 1
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_tx12_execute_tool_handling_is_idempotent_on_redelivery(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "read")
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    exhaust(seeded, run_id, "execute_tool")
    wakes_before = len(jobs(seeded, run_id, "advance_run"))
    # A worker leases the exhausted job and stalls; its lease expires and another worker takes it.
    first = await queue.lease_one(runtime.engine, kinds=[JobKind.EXECUTE_TOOL], lease_seconds=60)
    assert first is not None
    expire_leases(seeded, run_id)
    second = await queue.lease_one(runtime.engine, kinds=[JobKind.EXECUTE_TOOL], lease_seconds=60)
    assert second is not None
    # The stalled worker's TX12 with its stale lease rolls back as a whole.
    await execute_tool.dead_letter(runtime, first)
    assert jobs(seeded, run_id, "execute_tool") == [("leased", 7)]
    assert entry(seeded, run_id)[1] == "pending"
    assert "TOOL_OUTCOME" not in events(seeded, run_id)
    # The current lessee's TX12 commits once; repeating it changes nothing.
    await execute_tool.dead_letter(runtime, second)
    await execute_tool.dead_letter(runtime, second)
    assert jobs(seeded, run_id, "execute_tool") == [("dead", 7)]
    assert entry(seeded, run_id)[1] == "failed"
    assert events(seeded, run_id).count("TOOL_OUTCOME") == 1
    assert len(jobs(seeded, run_id, "advance_run")) == wakes_before + 1
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_tx12_advance_run_handling_is_idempotent_on_redelivery(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "read")
    rows(seeded, "UPDATE tether.jobs SET attempts = 8 WHERE run_id = %s RETURNING id", run_id)
    job = await queue.lease_one(runtime.engine, kinds=[JobKind.ADVANCE_RUN], lease_seconds=60)
    assert job is not None
    assert job.attempts == 9
    assert job.max_attempts == 8
    assert await advance_run.dead_letter(runtime, job) is advance_run.Outcome.COMPLETED
    after_first = generation(seeded, run_id)
    assert await advance_run.dead_letter(runtime, job) is advance_run.Outcome.COMPLETED
    assert generation(seeded, run_id) == after_first
    assert run_status(seeded, run_id) == ("failed", "system_error")
    assert events(seeded, run_id).count("DEAD_LETTERED") == 1
    assert events(seeded, run_id).count("RUN_FINISHED") == 1
    assert jobs(seeded, run_id, "advance_run") == [("dead", 9), ("ready", 0)]
    assert await chain_valid(seeded, tmp_path, run_id)
