"""Chaos harness (a bounded, in-process equivalent of PD-24, plus helpers for real processes).

Crashes are injected deterministically at named points with ``Crash``, a ``BaseException`` that
no handler catches: like a killed process, nothing after the point runs, the job keeps its lease
(until it is expired on purpose) and every open transaction rolls back. "Restarting" a worker is
opening a fresh ``WorkerRuntime`` (its own engine and pools); competing workers are several
runtimes driven concurrently. Waits poll database state, never sleep for an outcome.

After every scenario, ``assert_reconstructable`` checks the run against its authoritative rows
(plan T-12 v0) and ``assert_terminal_stable`` tries to reopen a terminal run every way the runtime
allows.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import yaml
from pydantic import BaseModel, SecretStr
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.storage.repos.audit import AuditRepository
from tether.storage.uow import UnitOfWork
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS, AGENT = "ws-a", "agent-1"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "abandoned"}

BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": AGENT, "workload": WORKLOAD.name}],
                "policy": {
                    "rules": [
                        {"tool": "get_service_health", "decision": "allow"},
                        {
                            "tool": "restart_service",
                            "decision": "require_approval",
                            "approver_roles": ["approver"],
                        },
                        {"tool": "probe_write", "decision": "allow"},
                        {"tool": "probe_nonidem", "decision": "allow"},
                    ],
                    "reconciliation": {"reconciler_roles": ["reconciler"]},
                },
            }
        ]
    }
)
SCENARIOS = {
    "remediate": (
        "name: remediate\nturns:\n"
        "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
        "  - tool_calls: [{tool: restart_service, arguments: {service: checkout}}]\n"
        "  - final: restarted\n"
    ),
    "write": (
        "name: write\nturns:\n"
        "  - tool_calls: [{tool: probe_write, arguments: {target: a}}]\n"
        "  - final: written\n"
    ),
    "nonidem": (
        "name: nonidem\nturns:\n"
        "  - tool_calls: [{tool: probe_nonidem, arguments: {target: a}}]\n"
        "  - final: written\n"
    ),
}


class Crash(BaseException):
    """A simulated process death, raised at an injection point: like a killed process, no
    handler runs after it (it is a ``BaseException``, which the runtime never catches)."""


def is_crash(error: BaseException) -> bool:
    if isinstance(error, Crash):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(is_crash(inner) for inner in error.exceptions)
    return error.__cause__ is not None and is_crash(error.__cause__)


# --- a counting target for test write tools -----------------------------------------------------


class ProbeIn(BaseModel):
    target: str


class ProbeOut(BaseModel):
    target: str


@dataclass
class Target:
    """Counts sends and applied effects. ``script`` decides each send: ``ok``; ``timeout`` (not
    applied, no answer); ``drop`` (applied, then the answer is lost). ``gate`` holds every send
    in flight until it is set."""

    script: list[str] = field(default_factory=list)
    sends: int = 0
    keys: list[str] = field(default_factory=list)
    effects: set[str] = field(default_factory=set)  # idempotent: applied once per key
    nonidem_effects: int = 0
    in_flight: int = 0
    gate: asyncio.Event | None = None

    async def _send(self, ctx: ToolContext) -> str:
        self.sends += 1
        self.keys.append(ctx.idempotency_key)
        self.in_flight += 1
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.in_flight -= 1
        return self.script.pop(0) if self.script else "ok"

    async def idem(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        step = await self._send(ctx)
        if step == "timeout":
            raise httpx.ReadTimeout("no response")
        if step == "crash_before":  # the worker dies while the target holds the request (C-05)
            raise Crash("killed with the write in flight, before the target applied it")
        self.effects.add(ctx.idempotency_key)
        if step == "crash_after":  # the target applied it; the worker dies before TX7 (C-06)
            raise Crash("killed after the target applied the write, before TX7")
        if step == "drop":
            raise httpx.RemoteProtocolError("connection dropped after the request was applied")
        return ProbeOut(target=args.target)

    async def nonidem(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        step = await self._send(ctx)
        if step == "crash_before":
            raise Crash("killed with the write in flight")
        self.nonidem_effects += 1
        if step == "drop":
            raise httpx.RemoteProtocolError("connection dropped after the request was applied")
        return ProbeOut(target=args.target)


TARGET = Target()


async def _idem(args: ProbeIn, ctx: ToolContext) -> ProbeOut:
    return await TARGET.idem(args, ctx)


async def _nonidem(args: ProbeIn, ctx: ToolContext) -> ProbeOut:
    return await TARGET.nonidem(args, ctx)


def _probe(name: str, idempotency: Any, handler: Any) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version="1.0.0",
        description="Chaos test write tool.",
        input_model=ProbeIn,
        output_model=ProbeOut,
        effect="write",
        idempotency=idempotency,
        handler=handler,
    )


PROBES = (
    _probe("probe_write", "supported", _idem),
    _probe("probe_nonidem", "unsupported", _nonidem),
)
TEST_WORKLOAD = Workload(
    name=WORKLOAD.name,
    tools=(*WORKLOAD.tools, *PROBES),
    agent=AgentDefinition(
        system_prompt="chaos", tools=(*WORKLOAD.agent.tools, *(p.name for p in PROBES))
    ),
)


# --- providers ----------------------------------------------------------------------------------


class CrashingProvider(ScriptedProvider):
    """Dies during the model call of ``turn`` the first ``times`` times (C-02)."""

    def __init__(self, scenarios: Path, *, turn: int, times: int = 1) -> None:
        super().__init__(scenarios, env="test")
        self.turn, self.left = turn, times

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        index = sum(1 for message in history if message.role == "assistant")
        if index == self.turn and self.left > 0:
            self.left -= 1
            raise Crash(f"killed during model turn {index}")
        return await super().complete(history, manifest)


class GatedProvider(ScriptedProvider):
    """Holds every model call until ``gate`` is set (a driver stuck mid-run)."""

    def __init__(self, scenarios: Path, gate: asyncio.Event) -> None:
        super().__init__(scenarios, env="test")
        self.gate = gate
        self.waiting = 0

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        self.waiting += 1
        await self.gate.wait()
        return await super().complete(history, manifest)


# --- crash injection after a committed transaction ----------------------------------------------


def crash_after_commit(
    monkeypatch: pytest.MonkeyPatch, event_type: str, *, skip: int = 0, times: int = 1
) -> None:
    """Kill the process right after the transaction that records ``event_type`` commits (after
    ``skip`` earlier occurrences), before anything else happens: the checkpoint, the job's
    completion, the HTTP response."""
    state = {"armed": False, "left": times, "skip": skip}
    append, exit_ = AuditRepository.append, UnitOfWork.__aexit__

    async def recording(self: AuditRepository, keys: Any, **kwargs: Any) -> Any:
        record = await append(self, keys, **kwargs)
        if kwargs.get("event_type") == event_type and record.created and state["left"] > 0:
            if state["skip"] > 0:
                state["skip"] -= 1
            else:
                state["armed"] = True
        return record

    async def exiting(self: UnitOfWork, exc_type: Any, exc: Any, tb: Any) -> None:
        armed, state["armed"] = state["armed"], False
        await exit_(self, exc_type, exc, tb)
        if armed and exc_type is None:
            state["left"] -= 1
            raise Crash(f"killed after the {event_type} transaction committed")

    monkeypatch.setattr(AuditRepository, "append", recording)
    monkeypatch.setattr(UnitOfWork, "__aexit__", exiting)


# --- the environment ----------------------------------------------------------------------------


@dataclass
class Env:
    db: Any
    tmp_path: Path

    def settings(self) -> Settings:
        bootstrap_file = self.tmp_path / "bootstrap.yaml"
        if not bootstrap_file.exists():
            bootstrap_file.write_text(
                yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8"
            )
        return Settings(
            database_url=SecretStr(self.db.app_dsn),
            env="test",
            bootstrap_file=bootstrap_file,
            scenarios_dir=self.tmp_path,
        )

    # -- workers --

    async def worker(self, provider: ScriptedProvider | None = None) -> WorkerRuntime:
        """A fresh worker process: its own engine, pools and in-memory state."""
        runtime = await WorkerRuntime.open(
            self.settings(),
            bootstrap=BOOT,
            provider=provider or ScriptedProvider(self.tmp_path, env="test"),
        )
        runtime.registries[WORKLOAD.name] = ToolRegistry(TEST_WORKLOAD)
        return runtime

    @staticmethod
    async def drain(*runtimes: WorkerRuntime, rounds: int = 200) -> None:
        """Every runtime processes jobs concurrently until none is due for any of them."""

        async def loop(runtime: WorkerRuntime) -> int:
            done = 0
            while await process_next_job(runtime):
                done += 1
            return done

        for _ in range(rounds):
            if not sum(await asyncio.gather(*(loop(r) for r in runtimes))):
                return
        raise AssertionError("the queue did not drain")

    def expire_leases(self) -> None:
        """A dead worker's leases run out (instead of waiting for the lease duration)."""
        self.sql(
            "UPDATE tether.jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
            " WHERE state = 'leased' RETURNING id"
        )

    # -- the api (a fresh app per request, like a restarted stateless api process) --

    async def call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        app = create_app(self.settings())
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
        ):
            return await http.request(method, url, **kwargs)

    async def submit(self, scenario: str) -> uuid.UUID:
        response = await self.call(
            "POST",
            "/v1/tasks/runs",
            json={
                "dev_identity": {
                    "workspace_id": WS,
                    "agent_id": AGENT,
                    "requester": {"sub": "alice", "roles": ["operator"]},
                },
                "task_input": {"message": f"chaos #scenario={scenario}"},
            },
        )
        assert response.status_code == 201, response.text
        return uuid.UUID(response.json()["run_id"])

    async def approve(self, approval_id: Any) -> httpx.Response:
        return await self.call(
            "POST",
            f"/v1/approvals/{approval_id}/approve",
            json={
                "dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]},
                "reason": "chaos approval",
            },
        )

    async def cancel(self, run_id: uuid.UUID) -> httpx.Response:
        return await self.call(
            "POST",
            f"/v1/tasks/runs/{run_id}/cancel",
            json={"dev_identity": {"workspace_id": WS, "sub": "alice", "roles": ["operator"]}},
        )

    async def reconcile(self, entry_id: Any, outcome: str) -> httpx.Response:
        return await self.call(
            "POST",
            f"/v1/ops/ledger/{entry_id}/reconcile",
            json={
                "dev_identity": {"workspace_id": WS, "sub": "dave", "roles": ["reconciler"]},
                "outcome": outcome,
                "justification": "checked the target",
            },
        )

    async def view(self, run_id: uuid.UUID) -> dict[str, Any]:
        response = await self.call(
            "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    # -- database --

    def sql(self, statement: str, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.migrator_dsn) as conn:
            return conn.execute(statement, params).fetchall()

    def status(self, run_id: uuid.UUID) -> str:
        return str(self.sql("SELECT status FROM tether.runs WHERE id = %s", run_id)[0][0])

    def approval_id(self, run_id: uuid.UUID) -> Any:
        [(approval_id,)] = self.sql("SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
        return approval_id

    def ledger(self, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
        """(step_ordinal, tool, state, attempts, resolution) per entry."""
        return self.sql(
            "SELECT step_ordinal, tool, state, attempts, resolution FROM tether.ledger_entries"
            " WHERE run_id = %s ORDER BY step_ordinal",
            run_id,
        )

    def events(self, run_id: uuid.UUID) -> list[str]:
        rows = self.sql(
            "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
        )
        return [event for (event,) in rows]

    def model_calls(self, run_id: uuid.UUID) -> int:
        return int(
            self.sql("SELECT count(*) FROM tether.model_calls WHERE run_id = %s", run_id)[0][0]
        )

    # -- invariants --

    async def assert_reconstructable(self, run_id: uuid.UUID) -> None:
        """T-12 v0: the authoritative rows tell a complete, consistent story."""
        view = await self.view(run_id)
        assert view["audit_chain_valid"] is True, run_id
        audit = self.sql(
            "SELECT seq, dedupe_key, event_type, payload_masked FROM tether.audit_events"
            " WHERE run_id = %s ORDER BY seq",
            run_id,
        )
        assert [seq for seq, *_ in audit] == list(range(1, len(audit) + 1))
        keys = {dedupe: (event, payload) for _, dedupe, event, payload in audit}
        assert keys["run_submitted"][0] == "RUN_SUBMITTED"
        entries = self.sql(
            "SELECT id, step_ordinal, state, attempts, resolution FROM tether.ledger_entries"
            " WHERE run_id = %s",
            run_id,
        )
        for entry_id, ordinal, state, attempts, resolution in entries:
            assert keys[f"decision:{ordinal}"][0] == "POLICY_DECISION"
            assert keys[f"authorized:{ordinal}"][0] == "EXECUTION_AUTHORIZED"
            dispatched = [k for k in keys if k.startswith(f"dispatched:{entry_id}:")]
            redispatched = [k for k in keys if k.startswith(f"redispatched:{entry_id}:")]
            assert len(dispatched) <= 1
            assert attempts == len(dispatched) + len(redispatched)
            if state not in ("pending", "dispatched"):
                assert keys[f"outcome:{entry_id}"][0] == "TOOL_OUTCOME"
            if resolution is not None:
                assert keys[f"reconciled:{entry_id}"][0] == "RECONCILED"
        status = self.status(run_id)
        if status in TERMINAL:
            assert keys["run_finished"][1]["status"] == status
            assert not [e for e in entries if e[2] in ("pending", "dispatched")]
        else:
            assert "run_finished" not in keys

    async def assert_terminal_stable(self, run_id: uuid.UUID, worker: WorkerRuntime) -> None:
        """A terminal run never reopens: wakes, timers, cancels and direct writes change nothing."""
        before = (self.status(run_id), self.events(run_id), self.ledger(run_id))
        assert before[0] in TERMINAL
        for kind, max_attempts in (("advance_run", 8), ("run_deadline", 10)):
            self.sql(
                "INSERT INTO tether.jobs (id, kind, workspace_id, run_id, max_attempts)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                uuid.uuid4(),
                kind,
                WS,
                run_id,
                max_attempts,
            )
        assert (await self.cancel(run_id)).status_code == 202
        await self.drain(worker)
        assert (self.status(run_id), self.events(run_id), self.ledger(run_id)) == before
        with psycopg.connect(self.db.app_dsn) as conn, pytest.raises(psycopg.Error):
            conn.execute("UPDATE tether.runs SET status = 'running' WHERE id = %s", (run_id,))


def make_env(db: Any, tmp_path: Path) -> Env:
    apply_bootstrap(db.migrator_dsn, BOOT)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    return Env(db, tmp_path)


def reset_target(gate: asyncio.Event | None = None) -> Target:
    TARGET.script.clear()
    TARGET.keys.clear()
    TARGET.effects.clear()
    TARGET.sends = TARGET.nonidem_effects = TARGET.in_flight = 0
    TARGET.gate = gate
    return TARGET


async def restarts() -> int:
    """The demo target's restart counter: the real side-effect count of ``restart_service``."""
    async with demo_client.http_client() as http:
        return int((await http.get("/services/checkout")).json()["restarts"])


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """Poll state (never sleep for an outcome) until ``predicate`` holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
