"""W5.2 ledger edge cases: an unresolved real-world effect is never hidden.

- Configuration unavailable mid-run (scope delta I-4): a worker that no longer has the run's
  agent (or workspace) makes no model call, authorization or send. While a ledger entry is
  unresolved the run is held for reconciliation (ADR-0018); once every entry is resolved it ends
  with the typed reason ``configuration_unavailable``, unless a stop decides (ADR-0006).
  Inspection and audit keep showing the action and how it was resolved.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

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
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.queue import backoff
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS, AGENT = "ws-le", "agent-le"
PERMISSIONS = ["kv:read", "kv:write"]
CONFIGURATION_UNAVAILABLE = "configuration_unavailable"


# --- a test-local key-value target --------------------------------------------------------------


class KeyIn(BaseModel):
    key: str


class SetIn(BaseModel):
    key: str
    value: str


class Value(BaseModel):
    key: str
    value: str


class Wrong(BaseModel):
    unexpected: int


@dataclass
class KvTarget:
    """``script`` decides each send: ``ok``; ``error`` (no answer, nothing applied); ``invalid``
    (the change is applied, then the answer violates the output schema)."""

    store: dict[str, str] = field(default_factory=dict)
    script: list[str] = field(default_factory=list)
    sends: int = 0
    applies: int = 0

    def reset(self) -> None:
        self.store = {"cfg": "a"}
        self.script.clear()
        self.sends = self.applies = 0

    def _step(self) -> str:
        self.sends += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "error":
            raise httpx.ReadTimeout("no response")
        return step

    async def get(self, args: KeyIn, ctx: ToolContext) -> BaseModel:
        step = self._step()
        if step == "invalid":
            return Wrong(unexpected=1)
        return Value(key=args.key, value=self.store.get(args.key, ""))

    async def set(self, args: SetIn, ctx: ToolContext) -> BaseModel:
        step = self._step()
        self.store[args.key] = args.value
        self.applies += 1
        if step == "invalid":
            return Wrong(unexpected=1)
        return Value(key=args.key, value=args.value)


TARGET = KvTarget()


async def _get(args: Any, ctx: ToolContext) -> BaseModel:
    return await TARGET.get(args, ctx)


async def _set(args: Any, ctx: ToolContext) -> BaseModel:
    return await TARGET.set(args, ctx)


def _tool(name: str, effect: Any, handler: Any, input_model: Any, **extra: Any) -> ToolDescriptor:
    values: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "description": f"Test-only {name}.",
        "input_model": input_model,
        "output_model": Value,
        "effect": effect,
        "handler": handler,
        "risk": "low",
        "required_permissions": ("kv:read",) if effect == "read" else ("kv:write",),
        **extra,
    }
    return ToolDescriptor(**values)


KV_GET = _tool("kv_get", "read", _get, KeyIn)
KV_GET_STRICT = _tool("kv_get_strict", "read", _get, KeyIn, on_exhausted="fail_run")
KV_SET = _tool("kv_set", "write", _set, SetIn, idempotency="supported")
KV_PUT = _tool("kv_put", "write", _set, SetIn, idempotency="unsupported")
KV_SET_GATED = _tool("kv_set_gated", "write", _set, SetIn, idempotency="supported", risk="high")
TOOLS = (KV_GET, KV_GET_STRICT, KV_SET, KV_PUT, KV_SET_GATED)


def workload(*tools: ToolDescriptor, retained: tuple[ToolDescriptor, ...] = ()) -> Workload:
    return Workload(
        name=WORKLOAD.name,
        tools=(*WORKLOAD.tools, *tools),
        agent=AgentDefinition(
            system_prompt="test", tools=(*WORKLOAD.agent.tools, *(t.name for t in tools))
        ),
        retained_tools=retained,
    )


TEST_WORKLOAD = workload(*TOOLS)
POLICY = {
    "rules": [
        *({"tool": t.name, "decision": "allow"} for t in TOOLS if t.risk == "low"),
        {"tool": "kv_set_gated", "decision": "require_approval", "approver_roles": ["approver"]},
    ],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
    "role_permissions": {"operator": PERMISSIONS},
    "agent_permissions": {AGENT: PERMISSIONS},
}
AGENTS = [{"id": AGENT, "workload": WORKLOAD.name}]
BOOT = BootstrapFile.model_validate(
    {"workspaces": [{"id": WS, "agents": AGENTS, "policy": POLICY}]}
)
# The same workspace after a deployment that removed the run's agent.
WITHOUT_AGENT = BootstrapFile.model_validate(
    {"workspaces": [{"id": WS, "agents": [], "policy": {**POLICY, "agent_permissions": {}}}]}
)
# A deployment that removed the whole workspace.
WITHOUT_WORKSPACE = BootstrapFile()


def scenario(name: str, *turns: Sequence[tuple[str, dict[str, Any]]]) -> str:
    lines = [f"name: {name}", "turns:"]
    for turn in turns:
        calls = [{"tool": tool, "arguments": arguments} for tool, arguments in turn]
        lines.append(f"  - tool_calls: {json.dumps(calls)}")
    lines.append(f"  - final: {name} done")
    return "\n".join(lines) + "\n"


SET_B = {"key": "cfg", "value": "b"}
SCENARIOS = {
    "get": scenario("get", [("kv_get", {"key": "cfg"})]),
    "strict": scenario("strict", [("kv_get_strict", {"key": "cfg"})]),
    "set": scenario("set", [("kv_set", SET_B)]),
    "put": scenario("put", [("kv_put", SET_B)]),
    "gated": scenario("gated", [("kv_set_gated", SET_B)]),
}


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


# --- fixtures and helpers -----------------------------------------------------------------------


def _settings(db: Any, tmp_path: Path, **extra: Any) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
        **extra,
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
    provider = RecordingProvider(ScriptedProvider(tmp_path, env="test"))
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path), bootstrap=BOOT, provider=provider
    )
    use(runtime, TEST_WORKLOAD)
    try:
        yield runtime
    finally:
        await runtime.close()


def use(runtime: WorkerRuntime, tools: Workload) -> None:
    """Deploy another set of tool versions to this worker (a restart with new workload code)."""
    runtime.registries[WORKLOAD.name] = ToolRegistry(tools)


async def call_api(
    seeded: Any, tmp_path: Path, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def submit(seeded: Any, tmp_path: Path, name: str) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": AGENT,
            "requester": {"sub": "alice", "roles": ["operator"]},
        },
        "task_input": {"message": f"kv #scenario={name}"},
    }
    response = await call_api(seeded, tmp_path, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def approve(seeded: Any, tmp_path: Path, approval_id: Any) -> None:
    body = {"dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]}}
    response = await call_api(
        seeded, tmp_path, "POST", f"/v1/approvals/{approval_id}/approve", json=body
    )
    assert response.status_code == 200, response.text


async def cancel(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> None:
    body = {"dev_identity": {"workspace_id": WS, "sub": "alice", "roles": ["operator"]}}
    url = f"/v1/tasks/runs/{run_id}/cancel"
    response = await call_api(seeded, tmp_path, "POST", url, json=body)
    assert response.status_code == 202, response.text


async def reconcile(seeded: Any, tmp_path: Path, entry_id: Any, outcome: str) -> None:
    body = {
        "dev_identity": {"workspace_id": WS, "sub": "dave", "roles": ["reconciler"]},
        "outcome": outcome,
        "justification": "checked the target",
    }
    response = await call_api(
        seeded, tmp_path, "POST", f"/v1/ops/ledger/{entry_id}/reconcile", json=body
    )
    assert response.status_code == 200, response.text


async def view(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> dict[str, Any]:
    response = await call_api(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def drain(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> None:
    for _ in range(80):
        if not await process_next_job(runtime, kinds):
            return
    raise AssertionError("the queue did not drain")


async def step(runtime: WorkerRuntime, kind: str) -> None:
    """Process exactly one due job of ``kind``."""
    assert await process_next_job(runtime, [kind]), f"no {kind} job was due"


def sql(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_status(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return sql(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


class Entry(NamedTuple):
    id: Any
    step_ordinal: int
    tool: str
    state: str
    outcome_class: str | None
    attempts: int
    outcome_reason: str | None
    resolution: str | None
    outcome_payload_masked: Any


def ledger(seeded: Any, run_id: uuid.UUID) -> list[Entry]:
    found = sql(
        seeded,
        "SELECT id, step_ordinal, tool, state, outcome_class, attempts, outcome_reason,"
        " resolution, outcome_payload_masked FROM tether.ledger_entries"
        " WHERE run_id = %s ORDER BY step_ordinal",
        run_id,
    )
    return [Entry(*row) for row in found]


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = sql(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event_type for (event_type,) in found]


def payload(seeded: Any, run_id: uuid.UUID, dedupe_key: str) -> dict[str, Any]:
    [(found,)] = sql(
        seeded,
        "SELECT payload_masked FROM tether.audit_events WHERE run_id = %s AND dedupe_key = %s",
        run_id,
        dedupe_key,
    )
    return dict(found)


def add_wake(seeded: Any, run_id: uuid.UUID) -> None:
    sql(
        seeded,
        "INSERT INTO tether.jobs (id, kind, workspace_id, run_id, max_attempts)"
        " VALUES (%s, 'advance_run', %s, %s, 8) RETURNING id",
        uuid.uuid4(),
        WS,
        run_id,
    )


def provider_of(runtime: WorkerRuntime) -> RecordingProvider:
    provider = runtime.provider
    assert isinstance(provider, RecordingProvider)
    return provider


def model_calls(runtime: WorkerRuntime) -> int:
    return len(provider_of(runtime).calls)


def tool_results(runtime: WorkerRuntime) -> list[tuple[str | None, Any]]:
    return [(m.outcome, m.content) for m in provider_of(runtime).calls[-1] if m.role == "tool"]


async def dispatched_after_one_failed_send(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, name: str
) -> tuple[uuid.UUID, Entry]:
    """Submit ``name``; its first send gets no answer, so the entry stays ``dispatched`` with a
    re-send due (or none, for a non-idempotent write: then it is already indeterminate)."""
    TARGET.script[:] = ["error"]
    run_id = await submit(seeded, tmp_path, name)
    await drain(runtime, ["advance_run"])
    await step(runtime, "execute_tool")
    [entry] = ledger(seeded, run_id)
    return run_id, entry


# --- I-4: configuration unavailable mid-run -----------------------------------------------------


@pytest.mark.parametrize("removed", [WITHOUT_AGENT, WITHOUT_WORKSPACE], ids=["agent", "workspace"])
@pytest.mark.asyncio
async def test_i4_without_an_unresolved_effect_the_run_fails_with_the_typed_reason(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, removed: BootstrapFile
) -> None:
    """Semantics (1) and (3): no model call; a typed configuration-unavailable reason."""
    run_id = await submit(seeded, tmp_path, "set")
    runtime.bootstrap = removed
    await drain(runtime)
    assert run_status(seeded, run_id) == ("failed", CONFIGURATION_UNAVAILABLE)
    assert model_calls(runtime) == 0
    assert ledger(seeded, run_id) == []
    assert events(seeded, run_id)[-1] == "RUN_FINISHED"
    assert payload(seeded, run_id, "run_finished") == {
        "status": "failed",
        "summary": CONFIGURATION_UNAVAILABLE,
    }
    assert (await view(seeded, tmp_path, run_id))["audit_chain_valid"] is True


@pytest.mark.asyncio
async def test_i4_a_run_waiting_on_an_approval_fails_typed_without_authorizing_anything(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "gated")
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "awaiting_approval"
    [(approval_id,)] = sql(seeded, "SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
    runtime.bootstrap = WITHOUT_AGENT
    await approve(seeded, tmp_path, approval_id)
    await drain(runtime)
    assert run_status(seeded, run_id) == ("failed", CONFIGURATION_UNAVAILABLE)
    assert ledger(seeded, run_id) == []
    assert "EXECUTION_AUTHORIZED" not in events(seeded, run_id)
    assert TARGET.sends == 0
    assert model_calls(runtime) == 1


@pytest.mark.asyncio
async def test_i4_a_pending_entry_holds_the_run_and_is_never_sent(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """Semantics (2)-(5): held while the entry is unresolved, nothing sent, then the typed
    failure once the entry has its honest outcome (nothing left Tether: not applied)."""
    run_id = await submit(seeded, tmp_path, "set")
    await drain(runtime, ["advance_run"])
    [entry] = ledger(seeded, run_id)
    assert entry.state == "pending"
    runtime.bootstrap = WITHOUT_AGENT
    add_wake(seeded, run_id)
    await drain(runtime, ["advance_run"])
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    assert "RUN_FINISHED" not in events(seeded, run_id)
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "not_applied")
    assert entry.outcome_reason == "not applied: agent is not configured"
    assert TARGET.sends == 0
    assert run_status(seeded, run_id) == ("failed", CONFIGURATION_UNAVAILABLE)
    assert model_calls(runtime) == 1


@pytest.mark.asyncio
async def test_i4_a_dispatched_write_is_held_until_reconciled_and_never_re_sent(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """The effect may have happened: its outcome is unknown, the run is held for a human, and
    the terminal record names the reconciled outcome (semantics 2, 4, 5 and 6)."""
    run_id, entry = await dispatched_after_one_failed_send(runtime, seeded, tmp_path, "set")
    assert (entry.state, entry.attempts) == ("dispatched", 1)
    runtime.bootstrap = WITHOUT_AGENT
    add_wake(seeded, run_id)
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("indeterminate", "unknown")
    assert entry.outcome_reason == "outcome unknown: agent is not configured; not re-sent"
    assert TARGET.sends == 1
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    assert "RUN_FINISHED" not in events(seeded, run_id)
    # Wakes while held change nothing.
    add_wake(seeded, run_id)
    await drain(runtime)
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    found = await view(seeded, tmp_path, run_id)
    assert [(e["tool"], e["state"]) for e in found["ledger"]] == [("kv_set", "indeterminate")]
    await reconcile(seeded, tmp_path, entry.id, "applied")
    await drain(runtime)
    assert run_status(seeded, run_id) == (
        "failed",
        f"{CONFIGURATION_UNAVAILABLE}; kv_set reconciled_applied",
    )
    assert TARGET.sends == 1
    assert model_calls(runtime) == 1
    found = await view(seeded, tmp_path, run_id)
    assert [(e["tool"], e["state"], e["resolution"]) for e in found["ledger"]] == [
        ("kv_set", "completed", "reconciled_applied")
    ]
    assert found["audit_chain_valid"] is True
    assert {"DISPATCHED", "TOOL_OUTCOME", "RECONCILED", "RUN_FINISHED"} <= set(
        events(seeded, run_id)
    )


@pytest.mark.asyncio
async def test_i4_a_dispatched_read_gets_a_typed_failure_then_the_run_fails_typed(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, entry = await dispatched_after_one_failed_send(runtime, seeded, tmp_path, "get")
    assert entry.state == "dispatched"
    runtime.bootstrap = WITHOUT_AGENT
    add_wake(seeded, run_id)
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "unknown")
    assert TARGET.sends == 1
    assert run_status(seeded, run_id) == ("failed", CONFIGURATION_UNAVAILABLE)


@pytest.mark.asyncio
async def test_i4_a_held_run_resumes_normally_once_the_configuration_is_back(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """The hold leaves the run's progress untouched: with its agent restored, the run continues
    from its recorded state."""
    run_id, _ = await dispatched_after_one_failed_send(runtime, seeded, tmp_path, "put")
    [entry] = ledger(seeded, run_id)
    assert entry.state == "indeterminate"
    runtime.bootstrap = WITHOUT_AGENT
    add_wake(seeded, run_id)
    await drain(runtime)
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    runtime.bootstrap = BOOT
    await reconcile(seeded, tmp_path, entry.id, "applied")
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "put done")
    assert model_calls(runtime) == 2
    assert tool_results(runtime) == [("applied", json.dumps({"resolution": "reconciled_applied"}))]


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_i4_a_stop_keeps_precedence_once_every_effect_is_resolved(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, stop: str
) -> None:
    run_id, _ = await dispatched_after_one_failed_send(runtime, seeded, tmp_path, "put")
    [entry] = ledger(seeded, run_id)
    runtime.bootstrap = WITHOUT_AGENT
    if stop == "cancel":
        await cancel(seeded, tmp_path, run_id)
    else:
        sql(
            seeded,
            "UPDATE tether.runs SET deadline_at = clock_timestamp() WHERE id = %s RETURNING id",
            run_id,
        )
        add_wake(seeded, run_id)
    await drain(runtime)
    # ADR-0006 rule 5: never cleanly stopped while an outcome is unknown.
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    await reconcile(seeded, tmp_path, entry.id, "not_applied")
    await drain(runtime)
    status, summary = run_status(seeded, run_id)
    assert status == ("cancelled" if stop == "cancel" else "timed_out")
    reason = "cancelled by alice" if stop == "cancel" else "deadline passed"
    assert summary == f"{reason}; kv_put reconciled_not_applied; {CONFIGURATION_UNAVAILABLE}"
    assert model_calls(runtime) == 1


# --- ADR-0011 rule 5: the pinned version gone once a ledger row exists --------------------------


def without(*names: str) -> Workload:
    """A deployment that no longer has (offers or retains) the named tools."""
    return workload(*(t for t in TOOLS if t.name not in names))


@pytest.mark.asyncio
async def test_adr0011_a_dispatched_write_whose_version_is_gone_is_indeterminate_not_diverged(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, entry = await dispatched_after_one_failed_send(runtime, seeded, tmp_path, "set")
    assert entry.state == "dispatched"
    use(runtime, without("kv_set"))
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class, entry.attempts) == ("indeterminate", "unknown", 1)
    assert entry.outcome_reason == "outcome unknown: tool version not resolvable; not re-sent"
    assert TARGET.sends == 1  # nothing re-sent
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    assert "REPLAY_DIVERGENCE" not in events(seeded, run_id)
    await reconcile(seeded, tmp_path, entry.id, "applied")
    await drain(runtime)
    # A deployment is not a divergence: the run continues with the reconciled outcome.
    assert run_status(seeded, run_id) == ("succeeded", "set done")
    assert tool_results(runtime) == [("applied", json.dumps({"resolution": "reconciled_applied"}))]
    assert "REPLAY_DIVERGENCE" not in events(seeded, run_id)
    assert (await view(seeded, tmp_path, run_id))["audit_chain_valid"] is True


@pytest.mark.asyncio
async def test_adr0011_a_dispatched_read_whose_version_is_gone_is_a_typed_failure(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, entry = await dispatched_after_one_failed_send(runtime, seeded, tmp_path, "get")
    assert entry.state == "dispatched"
    use(runtime, without("kv_get"))
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "unknown")
    assert TARGET.sends == 1
    reason = "outcome unknown: tool version not resolvable; not re-sent"
    assert tool_results(runtime) == [("failed", reason)]
    assert run_status(seeded, run_id) == ("succeeded", "get done")
    assert "REPLAY_DIVERGENCE" not in events(seeded, run_id)


@pytest.mark.asyncio
async def test_adr0011_a_pending_entry_whose_version_is_gone_is_not_applied_and_the_run_goes_on(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "set")
    await drain(runtime, ["advance_run"])
    assert [e.state for e in ledger(seeded, run_id)] == ["pending"]
    use(runtime, without("kv_set"))
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "not_applied")
    assert TARGET.sends == 0
    assert tool_results(runtime) == [("not_applied", "not applied: tool version not resolvable")]
    assert run_status(seeded, run_id) == ("succeeded", "set done")
    assert "REPLAY_DIVERGENCE" not in events(seeded, run_id)


@pytest.mark.asyncio
async def test_adr0011_a_version_gone_never_excuses_rows_that_disagree(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """Without the descriptor the persisted rows must agree; a ledger row whose hash differs
    from the call's decision is still a divergence, and nothing is sent."""
    run_id, entry = await dispatched_after_one_failed_send(runtime, seeded, tmp_path, "set")
    sql(
        seeded,
        "UPDATE tether.ledger_entries SET call_hash = %s WHERE id = %s RETURNING id",
        "d" * 64,
        entry.id,
    )
    use(runtime, without("kv_set"))
    add_wake(seeded, run_id)
    await drain(runtime)
    assert "REPLAY_DIVERGENCE" in events(seeded, run_id)
    assert TARGET.sends == 1
    # The divergence drains: the unresolved write holds the run until a human resolves it.
    [entry] = ledger(seeded, run_id)
    assert entry.state == "indeterminate"
    assert run_status(seeded, run_id) == ("awaiting_reconciliation", None)
    await reconcile(seeded, tmp_path, entry.id, "not_applied")
    await drain(runtime)
    assert run_status(seeded, run_id) == ("failed", "replay_divergence")
    assert model_calls(runtime) == 1
