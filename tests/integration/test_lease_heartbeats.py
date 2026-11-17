"""Lease heartbeats (W6.1, T-29; ADR-0004 rule 2, ADR-0005 rule 5, ADR-0021 rule 6).

A worker extends the lease of the job it is processing, conditional on its lease token. A job
that outlives its lease with a live heartbeat is neither redelivered nor turned ``indeterminate``;
a lost lease aborts the handler, which is exactly a worker dying: nothing is recorded by the
aborted delivery and the next delivery applies the existing rules.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
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

import tether.runtime.driver as driver_module
from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.queue import backoff
from tether.queue import postgres as queue
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS = "ws-h"
LEASE = 1.5
INTERVAL = 0.3
LONG = 4.0  # a send that outlives the lease more than twice over
POLICY = {
    "rules": [{"tool": name, "decision": "allow"} for name in ("probe_idem", "probe_nonidem")],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
    "role_permissions": {"operator": ["probe:write"]},
    "agent_permissions": {"agent-h": ["probe:write"]},
}
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": "agent-h", "workload": "incident_remediation"}],
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
    for name in ("idem", "nonidem")
}


class ProbeIn(BaseModel):
    target: str


class ProbeOut(BaseModel):
    target: str
    applied: bool


@dataclass
class Target:
    """``script``: ``long`` = answers after ``LONG`` seconds; ``hang`` = never answers;
    anything else answers at once."""

    script: list[str] = field(default_factory=list)
    sends: int = 0
    keys: list[str] = field(default_factory=list)
    effects: int = 0
    idem_effects: set[str] = field(default_factory=set)
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def _step(self, ctx: ToolContext) -> None:
        self.sends += 1
        self.keys.append(ctx.idempotency_key)
        self.started.set()
        step = self.script.pop(0) if self.script else "ok"
        if step == "long":
            await asyncio.sleep(LONG)
        elif step == "hang":
            await asyncio.sleep(3600)

    async def idem(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        await self._step(ctx)
        self.idem_effects.add(ctx.idempotency_key)
        self.effects += 1
        return ProbeOut(target=args.target, applied=True)

    async def nonidem(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        await self._step(ctx)
        self.effects += 1
        return ProbeOut(target=args.target, applied=True)

    def reset(self) -> None:
        self.script.clear()
        self.keys.clear()
        self.idem_effects.clear()
        self.sends = self.effects = 0
        self.started = asyncio.Event()


TARGET = Target()


def _tool(name: str, idempotency: Any, handler: Any, **extra: Any) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version="1.0.0",
        description=f"Test-only {name}.",
        input_model=ProbeIn,
        output_model=ProbeOut,
        effect="write",
        idempotency=idempotency,
        handler=handler,
        risk="low",
        required_permissions=("probe:write",),
        **extra,
    )


def _workload(**extra: Any) -> Workload:
    probes = (
        _tool("probe_idem", "supported", TARGET.idem, **extra),
        _tool("probe_nonidem", "unsupported", TARGET.nonidem, **extra),
    )
    return Workload(
        name=WORKLOAD.name,
        tools=(*WORKLOAD.tools, *probes),
        agent=AgentDefinition(
            system_prompt="test", tools=(*WORKLOAD.agent.tools, *(p.name for p in probes))
        ),
    )


def _settings(db: Any, tmp_path: Path, **overrides: Any) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    values: dict[str, Any] = {
        "worker_lease_seconds": LEASE,
        "worker_lease_heartbeat_seconds": INTERVAL,
    }
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
        **{**values, **overrides},
    )


@pytest.fixture(autouse=True)
def fake_target(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path),
        bootstrap=BOOT,
        provider=ScriptedProvider(tmp_path, env="test"),
    )
    # Every tool's timeout (default 10 s) is far above the lease: allowed once heartbeats exist.
    runtime.registries[WORKLOAD.name] = ToolRegistry(_workload())
    try:
        yield runtime
    finally:
        await runtime.close()


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


async def submit(seeded: Any, tmp_path: Path, scenario: str) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": "agent-h",
            "requester": {"sub": "alice", "roles": ["operator"]},
        },
        "task_input": {"message": f"probe #scenario={scenario}"},
    }
    app = create_app(_settings(seeded.db, tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        response = await http.post("/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


def tool_job(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    """(state, attempts, lease_token) of the run's ``execute_tool`` job."""
    [job] = rows(
        seeded,
        "SELECT state, attempts, lease_token FROM tether.jobs"
        " WHERE run_id = %s AND kind = 'execute_tool'",
        run_id,
    )
    return job


def entry(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    [found] = rows(
        seeded,
        "SELECT state, attempts, outcome_class FROM tether.ledger_entries WHERE run_id = %s",
        run_id,
    )
    return found


def status(seeded: Any, run_id: uuid.UUID) -> str:
    return str(rows(seeded, "SELECT status FROM tether.runs WHERE id = %s", run_id)[0][0])


def expire_leases(seeded: Any, run_id: uuid.UUID) -> None:
    rows(
        seeded,
        "UPDATE tether.jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
        " WHERE run_id = %s AND state = 'leased' RETURNING id",
        run_id,
    )


def steal_lease(seeded: Any, run_id: uuid.UUID) -> uuid.UUID:
    """Another lessee holds the job now: our token no longer matches."""
    token = uuid.uuid4()
    rows(
        seeded,
        "UPDATE tether.jobs SET lease_token = %s WHERE run_id = %s AND kind = 'execute_tool'"
        " AND state = 'leased' RETURNING id",
        token,
        run_id,
    )
    return token


async def drain_all(runtime: WorkerRuntime) -> None:
    for _ in range(60):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


async def start_slow_send(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, scenario: str, step: str
) -> tuple[uuid.UUID, asyncio.Task[bool]]:
    """Run the job up to its ``execute_tool`` send, which is left in flight."""
    TARGET.script[:] = [step]
    run_id = await submit(seeded, tmp_path, scenario)
    while await process_next_job(runtime, ["advance_run"]):
        pass
    assert tool_job(seeded, run_id)[0] == "ready"
    task = asyncio.create_task(process_next_job(runtime, ["execute_tool"]))
    await asyncio.wait_for(TARGET.started.wait(), 10)
    return run_id, task


# --- criteria 1 and 5: a long job with a live heartbeat -------------------------------------


@pytest.mark.asyncio
async def test_t29_a_long_non_idempotent_write_with_a_heartbeat_completes_once(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, task = await start_slow_send(runtime, seeded, tmp_path, "nonidem", "long")
    # Past the original lease, nothing is left to lease for this job.
    await asyncio.sleep(LEASE * 1.5)
    assert tool_job(seeded, run_id)[0] == "leased"
    stolen = await queue.lease_one(runtime.engine, kinds=["execute_tool"], lease_seconds=LEASE)
    assert stolen is None
    assert await task is True
    assert (TARGET.sends, TARGET.effects) == (1, 1)
    state, attempts, _ = tool_job(seeded, run_id)
    assert (state, attempts) == ("done", 1)
    assert entry(seeded, run_id) == ("completed", 1, "applied")
    await drain_all(runtime)
    assert status(seeded, run_id) == "succeeded"


@pytest.mark.asyncio
async def test_t29_a_slow_advance_run_keeps_its_lease_and_is_not_redelivered(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    provider = runtime.provider
    inner = provider.complete

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(LEASE * 2)
        return await inner(*args, **kwargs)

    provider.complete = slow  # type: ignore[method-assign]
    run_id = await submit(seeded, tmp_path, "idem")
    task = asyncio.create_task(process_next_job(runtime, ["advance_run"]))
    await asyncio.sleep(LEASE * 1.4)
    [(state, attempts)] = rows(
        seeded,
        "SELECT state, attempts FROM tether.jobs WHERE run_id = %s AND kind = 'advance_run'",
        run_id,
    )
    assert (state, attempts) == ("leased", 1)
    assert await queue.lease_one(runtime.engine, kinds=["advance_run"], lease_seconds=LEASE) is None
    assert await task is True
    [(attempts,)] = rows(
        seeded,
        "SELECT attempts FROM tether.jobs WHERE run_id = %s AND kind = 'advance_run'"
        " ORDER BY created_at LIMIT 1",
        run_id,
    )
    assert attempts == 1


# --- criterion 3: the queue primitive -------------------------------------------------------


@pytest.mark.asyncio
async def test_t29_a_heartbeat_with_a_stale_token_changes_nothing_and_reports_the_loss(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    await submit(seeded, tmp_path, "idem")
    job = await queue.lease_one(runtime.engine, kinds=["advance_run"], lease_seconds=LEASE)
    assert job is not None
    assert await queue.heartbeat(runtime.engine, job, lease_seconds=60.0) is True
    [(extended,)] = rows(
        seeded,
        "SELECT lease_expires_at - clock_timestamp() > interval '30 seconds' FROM tether.jobs"
        " WHERE id = %s",
        job.id,
    )
    assert extended is True
    # Another lessee takes over (its token replaces ours).
    rows(
        seeded,
        "UPDATE tether.jobs SET lease_token = %s WHERE id = %s RETURNING id",
        uuid.uuid4(),
        job.id,
    )
    query = "SELECT lease_token, lease_expires_at FROM tether.jobs WHERE id = %s"
    before = rows(seeded, query, job.id)
    assert await queue.heartbeat(runtime.engine, job, lease_seconds=60.0) is False
    assert rows(seeded, query, job.id) == before


# --- criteria 2 and 4: a lost lease is a worker death ---------------------------------------


@pytest.mark.asyncio
async def test_t29_a_lost_lease_aborts_a_non_idempotent_write_which_is_never_re_sent(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, task = await start_slow_send(runtime, seeded, tmp_path, "nonidem", "hang")
    steal_lease(seeded, run_id)
    await asyncio.wait_for(task, 10)
    # The aborted delivery recorded nothing and did not complete or release the job.
    assert entry(seeded, run_id)[0] == "dispatched"
    state, attempts, token = tool_job(seeded, run_id)
    assert (state, attempts) == ("leased", 1)
    assert token is not None
    # The other lessee's lease runs out, as a dead worker's would: the next delivery decides.
    expire_leases(seeded, run_id)
    await drain_all(runtime)
    assert entry(seeded, run_id) == ("indeterminate", 1, "unknown")
    assert TARGET.sends == 1
    assert status(seeded, run_id) == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_t29_a_lost_lease_aborts_an_idempotent_write_which_is_re_sent_with_its_key(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, task = await start_slow_send(runtime, seeded, tmp_path, "idem", "hang")
    steal_lease(seeded, run_id)
    await asyncio.wait_for(task, 10)
    assert entry(seeded, run_id)[0] == "dispatched"
    expire_leases(seeded, run_id)
    await drain_all(runtime)
    assert TARGET.sends == 2
    assert len(TARGET.idem_effects) == 1
    assert len(set(TARGET.keys)) == 1
    assert entry(seeded, run_id)[0] == "completed"
    assert status(seeded, run_id) == "succeeded"


@pytest.mark.asyncio
async def test_t29_an_aborted_job_is_not_completed_or_released_by_the_aborted_worker(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, task = await start_slow_send(runtime, seeded, tmp_path, "idem", "hang")
    replacement = steal_lease(seeded, run_id)
    await asyncio.wait_for(task, 10)
    state, _, token = tool_job(seeded, run_id)
    assert (state, token) == ("leased", replacement)


# --- criterion 7: the startup restriction is lifted -----------------------------------------


@pytest.mark.asyncio
async def test_w6_1_a_worker_starts_with_a_tool_timeout_at_or_above_the_lease(
    seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = _workload(timeout_seconds=LEASE * 4)
    monkeypatch.setattr(driver_module, "load_workload", lambda name: slow)
    runtime = await WorkerRuntime.open(_settings(seeded.db, tmp_path), bootstrap=BOOT)
    await runtime.close()
