"""W3.2 runtime evidence for the governed tool contract (ADR-0021).

- T-04: ``on_exhausted``. ``typed_failure`` hands the model a typed ``failed`` result;
  ``fail_run`` is a terminating result (ADR-0012 rule 6): no further model call and no
  ``CALL_SKIPPED`` events. An active stop wins, and the run is never terminalized while any
  ledger entry is unresolved.
- T-21: preconditions are declared argument fields, bound into the call hash and the approval
  (masked), and enforced atomically by the target. A definitive rejection is ``not_applied`` and
  changes nothing; an error or timeout while the target checks is ``unknown``.
- Pinned versions: every lookup after TX2 resolves the pinned ``(name, version)``; a retained
  version executes; a version gone before a ledger row exists is a typed ``not_applied``, never a
  replay divergence and never a newer version.
- Outcome classification (migration ``0004``): ``applied``, ``not_applied``, ``unknown``,
  ``failed``; historical rows stay unclassified.

The target is a test-local compare-and-swap store (T-21 fixture; the demo target's
``update_service_config`` is W9.3).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, NamedTuple

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from alembic import command
from alembic.config import Config
from psycopg import errors
from pydantic import BaseModel, SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.cli.main import MIGRATIONS_LOCATION, upgrade_to_head
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.queue import backoff
from tether.registry.masking import MASK
from tether.registry.registry import ToolRegistry
from tether.runtime import driver as driver_module
from tether.runtime.driver import WorkerRuntime
from tether.runtime.graph import call_hash_for
from tether.sdk import (
    AgentDefinition,
    Sensitive,
    ToolContext,
    ToolDescriptor,
    ToolNotApplied,
    Workload,
)
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS, AGENT = "ws-tc", "agent-tc"
PERMISSIONS = ["cas:read", "cas:write"]
ETAG = "s3cr3t-etag-value"


# --- a test-local compare-and-swap target ------------------------------------------------------


class KeyIn(BaseModel):
    key: str


class SetIn(BaseModel):
    key: str
    value: str
    expected_version: int


class GatedIn(BaseModel):
    key: str
    value: str
    expected_version: int
    expected_etag: Annotated[str, Sensitive]


class Value(BaseModel):
    key: str
    version: int
    value: str


class Wrong(BaseModel):
    unexpected: int


@dataclass
class CasTarget:
    """Each key has a version. A write applies only if its expected version (and etag) still
    match, checked atomically with the change; otherwise it applies nothing and rejects.
    ``script`` decides each send: ``ok``; ``error`` (no answer, nothing applied); ``slow`` (no
    answer within the tool's timeout, nothing applied); ``invalid`` (a malformed read answer)."""

    store: dict[str, tuple[int, str]] = field(default_factory=dict)
    script: list[str] = field(default_factory=list)
    sends: int = 0
    applies: int = 0
    keys: list[str] = field(default_factory=list)
    results: dict[str, Value] = field(default_factory=dict)
    seen_etags: list[str] = field(default_factory=list)
    executed_versions: list[str] = field(default_factory=list)
    hook: Callable[[], Awaitable[None]] | None = None

    def reset(self) -> None:
        self.store = {"cfg": (7, "a")}
        self.script.clear()
        self.keys.clear()
        self.results.clear()
        self.seen_etags.clear()
        self.executed_versions.clear()
        self.sends = self.applies = 0
        self.hook = None

    async def _step(self, ctx: ToolContext) -> str:
        self.sends += 1
        self.keys.append(ctx.idempotency_key)
        if self.hook is not None:
            await self.hook()
        step = self.script.pop(0) if self.script else "ok"
        if step == "error":
            raise httpx.ReadTimeout("no response")
        if step == "slow":
            await asyncio.sleep(30)
        return step

    def _cas(self, key: str, expected_version: int, value: str) -> Value:
        version, _ = self.store.get(key, (0, ""))
        if expected_version != version:
            raise ToolNotApplied(f"precondition failed: version is {version}")
        self.store[key] = (version + 1, value)
        self.applies += 1
        return Value(key=key, version=version + 1, value=value)

    async def get(self, args: KeyIn, ctx: ToolContext) -> BaseModel:
        step = await self._step(ctx)
        if args.key not in self.store:
            raise ToolNotApplied("no such key")
        if step == "invalid":
            return Wrong(unexpected=1)
        version, value = self.store[args.key]
        return Value(key=args.key, version=version, value=value)

    async def set(self, args: SetIn, ctx: ToolContext) -> Value:
        await self._step(ctx)
        if ctx.idempotency_key in self.results:  # the target dedupes by key
            return self.results[ctx.idempotency_key]
        result = self._cas(args.key, args.expected_version, args.value)
        self.results[ctx.idempotency_key] = result
        return result

    async def put(self, args: SetIn, ctx: ToolContext) -> Value:
        await self._step(ctx)
        self.applies += 1
        return Value(key=args.key, version=0, value=args.value)

    async def set_gated(self, args: GatedIn, ctx: ToolContext, version: str) -> Value:
        await self._step(ctx)
        self.executed_versions.append(version)
        self.seen_etags.append(args.expected_etag)
        if args.expected_etag != ETAG:
            raise ToolNotApplied("precondition failed: etag")
        return self._cas(args.key, args.expected_version, args.value)


TARGET = CasTarget()


async def _get(args: Any, ctx: ToolContext) -> BaseModel:
    return await TARGET.get(args, ctx)


async def _set(args: Any, ctx: ToolContext) -> Value:
    return await TARGET.set(args, ctx)


async def _put(args: Any, ctx: ToolContext) -> Value:
    return await TARGET.put(args, ctx)


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
        "required_permissions": ("cas:read",) if effect == "read" else ("cas:write",),
        **extra,
    }
    return ToolDescriptor(**values)


def gated(version: str, permissions: tuple[str, ...] = ("cas:write",)) -> ToolDescriptor:
    async def handler(args: Any, ctx: ToolContext) -> Value:
        return await TARGET.set_gated(args, ctx, version)

    return _tool(
        "cas_set_gated",
        "write",
        handler,
        GatedIn,
        version=version,
        idempotency="supported",
        risk="high",
        required_permissions=permissions,
        preconditions=("expected_version", "expected_etag"),
    )


CAS_GET = _tool("cas_get", "read", _get, KeyIn)
CAS_GET_STRICT = _tool("cas_get_strict", "read", _get, KeyIn, on_exhausted="fail_run")
CAS_GET_SLOW = _tool("cas_get_slow", "read", _get, KeyIn, timeout_seconds=0.2)
CAS_SET = _tool(
    "cas_set",
    "write",
    _set,
    SetIn,
    idempotency="supported",
    preconditions=("expected_version",),
    timeout_seconds=0.5,
)
CAS_PUT = _tool("cas_put", "write", _put, SetIn, idempotency="unsupported", timeout_seconds=0.2)
GATED_V1 = gated("1.0.0")
OTHERS = (CAS_GET, CAS_GET_STRICT, CAS_GET_SLOW, CAS_SET, CAS_PUT)


def workload(*tools: ToolDescriptor, retained: tuple[ToolDescriptor, ...] = ()) -> Workload:
    return Workload(
        name=WORKLOAD.name,
        tools=(*WORKLOAD.tools, *tools),
        agent=AgentDefinition(
            system_prompt="test", tools=(*WORKLOAD.agent.tools, *(t.name for t in tools))
        ),
        retained_tools=retained,
    )


TEST_WORKLOAD = workload(*OTHERS, GATED_V1)

POLICY = {
    "rules": [
        *({"tool": t.name, "decision": "allow"} for t in OTHERS),
        {"tool": "cas_set_gated", "decision": "require_approval", "approver_roles": ["approver"]},
    ],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
    "role_permissions": {"operator": PERMISSIONS},
    "agent_permissions": {AGENT: PERMISSIONS},
}
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": AGENT, "workload": WORKLOAD.name}],
                "policy": POLICY,
            }
        ]
    }
)


def scenario(name: str, *turns: Sequence[tuple[str, dict[str, Any]]]) -> str:
    lines = [f"name: {name}", "turns:"]
    for turn in turns:
        calls = [{"tool": tool, "arguments": arguments} for tool, arguments in turn]
        lines.append(f"  - tool_calls: {json.dumps(calls)}")
    lines.append(f"  - final: {name} done")
    return "\n".join(lines) + "\n"


SET_7 = {"key": "cfg", "value": "b", "expected_version": 7}
SET_6 = {**SET_7, "expected_version": 6}
GATED_7 = {**SET_7, "expected_etag": ETAG}
GATED_6 = {**GATED_7, "expected_version": 6}
SCENARIOS = {
    "get": scenario("get", [("cas_get", {"key": "cfg"})]),
    "slow_get": scenario("slow_get", [("cas_get_slow", {"key": "cfg"})]),
    "strict": scenario("strict", [("cas_get_strict", {"key": "cfg"}), ("cas_set", SET_7)]),
    "strict_only": scenario("strict_only", [("cas_get_strict", {"key": "cfg"})]),
    "strict_missing": scenario("strict_missing", [("cas_get_strict", {"key": "nope"})]),
    "set": scenario("set", [("cas_set", SET_7)]),
    "set_stale": scenario("set_stale", [("cas_set", SET_6)]),
    "put": scenario("put", [("cas_put", SET_7)]),
    "gated": scenario("gated", [("cas_set_gated", GATED_7)]),
    "gated_retry": scenario(
        "gated_retry", [("cas_set_gated", GATED_6)], [("cas_set_gated", GATED_7)]
    ),
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
        "task_input": {"message": f"cas #scenario={name}"},
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


async def drain(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> None:
    for _ in range(80):
        if not await process_next_job(runtime, kinds):
            return
    raise AssertionError("the queue did not drain")


def sql(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_status(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return sql(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


class Entry(NamedTuple):
    id: Any
    step_ordinal: int
    tool: str
    tool_version: str
    state: str
    outcome_class: str | None
    attempts: int
    outcome_reason: str | None
    idempotency_key: str
    call_hash: str


def ledger(seeded: Any, run_id: uuid.UUID) -> list[Entry]:
    """The run's entries, except the ones a test seeds at ordinal 99."""
    found = sql(
        seeded,
        "SELECT id, step_ordinal, tool, tool_version, state, outcome_class, attempts,"
        " outcome_reason, idempotency_key, call_hash FROM tether.ledger_entries"
        " WHERE run_id = %s AND step_ordinal < 90 ORDER BY step_ordinal",
        run_id,
    )
    return [Entry(*row) for row in found]


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = sql(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event_type for (event_type,) in found]


def dedupe_keys(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = sql(
        seeded, "SELECT dedupe_key FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [key for (key,) in found]


def payload(seeded: Any, run_id: uuid.UUID, dedupe_key: str) -> dict[str, Any]:
    [(found,)] = sql(
        seeded,
        "SELECT payload_masked FROM tether.audit_events WHERE run_id = %s AND dedupe_key = %s",
        run_id,
        dedupe_key,
    )
    return dict(found)


def approvals(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return sql(
        seeded,
        "SELECT id, step_ordinal, call_hash, tool_version, arguments_masked, preconditions_masked"
        " FROM tether.approvals WHERE run_id = %s ORDER BY step_ordinal",
        run_id,
    )


def provider_of(runtime: WorkerRuntime) -> RecordingProvider:
    provider = runtime.provider
    assert isinstance(provider, RecordingProvider)
    return provider


def tool_results(runtime: WorkerRuntime) -> list[tuple[str | None, Any]]:
    return [(m.outcome, m.content) for m in provider_of(runtime).calls[-1] if m.role == "tool"]


def run_row(seeded: Any, run_id: uuid.UUID) -> dict[str, Any]:
    [(workspace_id, agent_id, binding_key_id)] = sql(
        seeded,
        "SELECT workspace_id, agent_id, binding_key_id FROM tether.runs WHERE id = %s",
        run_id,
    )
    return {
        "id": str(run_id),
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "binding_key_id": binding_key_id,
    }


def release_retries(seeded: Any, run_id: uuid.UUID) -> None:
    sql(
        seeded,
        "UPDATE tether.jobs SET run_after = clock_timestamp()"
        " WHERE run_id = %s AND kind = 'execute_tool' AND state = 'ready' RETURNING id",
        run_id,
    )


def exhaust_tool_job(seeded: Any, run_id: uuid.UUID) -> None:
    sql(
        seeded,
        "UPDATE tether.jobs SET attempts = max_attempts"
        " WHERE run_id = %s AND kind = 'execute_tool' AND state = 'ready' RETURNING id",
        run_id,
    )


def add_wake(seeded: Any, run_id: uuid.UUID) -> None:
    sql(
        seeded,
        "INSERT INTO tether.jobs (id, kind, workspace_id, run_id, max_attempts)"
        " VALUES (%s, 'advance_run', %s, %s, 8) RETURNING id",
        uuid.uuid4(),
        WS,
        run_id,
    )


def seed_unresolved(seeded: Any, run_id: uuid.UUID, state: str) -> uuid.UUID:
    """Another entry of the same run whose real-world outcome is not known yet."""
    entry_id = uuid.uuid4()
    sql(
        seeded,
        "INSERT INTO tether.ledger_entries (id, workspace_id, run_id, step_ordinal, tool,"
        " tool_version, effect, idempotency_support, call_hash, idempotency_key, state, attempts,"
        " outcome_class)"
        " VALUES (%s, %s, %s, 99, 'cas_set', '1.0.0', 'write', 'idempotent', %s, %s, %s, 1, %s)"
        " RETURNING id",
        entry_id,
        WS,
        run_id,
        "c" * 64,
        "tth_" + "c" * 52,
        state,
        "unknown" if state == "indeterminate" else None,
    )
    return entry_id


async def chain_valid(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> bool:
    response = await call_api(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    assert response.status_code == 200, response.text
    return bool(response.json()["audit_chain_valid"])


# --- T-04: on_exhausted -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t04_typed_failure_exhaustion_hands_the_model_a_typed_failed_result(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["error"] * 3
    run_id = await submit(seeded, tmp_path, "get")
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class, entry.attempts) == ("failed", "unknown", 3)
    assert TARGET.sends == 3
    assert [outcome for outcome, _ in tool_results(runtime)] == ["failed"]
    assert run_status(seeded, run_id) == ("succeeded", "get done")


@pytest.mark.asyncio
async def test_t04_fail_run_exhaustion_terminates_without_skip_events_or_another_model_call(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """ADR-0012 rule 6: the remaining call of the turn is neither governed nor marked skipped."""
    TARGET.script[:] = ["error"] * 3
    run_id = await submit(seeded, tmp_path, "strict")
    await drain(runtime)
    assert run_status(seeded, run_id) == ("failed", "tool_exhausted:cas_get_strict")
    [entry] = ledger(seeded, run_id)
    assert (entry.step_ordinal, entry.tool, entry.state, entry.outcome_class) == (
        0,
        "cas_get_strict",
        "failed",
        "unknown",
    )
    keys = dedupe_keys(seeded, run_id)
    assert "CALL_SKIPPED" not in events(seeded, run_id)
    assert "decision:1" not in keys
    assert not [k for k in keys if k.startswith("skipped:")]
    assert events(seeded, run_id)[-1] == "RUN_FINISHED"
    assert len(provider_of(runtime).calls) == 1  # no model call after the terminating result
    assert (TARGET.sends, TARGET.applies) == (3, 0)
    assert await chain_valid(seeded, tmp_path, run_id)
    await drain(runtime)
    assert run_status(seeded, run_id) == ("failed", "tool_exhausted:cas_get_strict")


@pytest.mark.asyncio
async def test_t04_fail_run_also_triggers_when_tx12_resolves_a_dispatched_read(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "strict_only")
    await drain(runtime, ["advance_run"])
    sql(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'dispatched', attempts = 1"
        " WHERE run_id = %s RETURNING id",
        run_id,
    )
    exhaust_tool_job(seeded, run_id)
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "unknown")
    assert entry.outcome_reason == "outcome unknown: delivery exhausted"
    assert run_status(seeded, run_id) == ("failed", "tool_exhausted:cas_get_strict")
    assert TARGET.sends == 0


@pytest.mark.parametrize(
    ("name", "script", "pending_dead_letter", "outcome_class", "model_outcome"),
    [
        # A definitive rejection by the target: positive evidence, not exhaustion.
        ("strict_missing", [], False, "not_applied", "not_applied"),
        # A malformed read answer: a definitive ordinary failure, never retried.
        ("strict_only", ["invalid"], False, "failed", "failed"),
        # Delivery exhausted before anything was sent: not applied, not exhaustion of sends.
        ("strict_only", [], True, "not_applied", "not_applied"),
    ],
    ids=["tool-not-applied", "invalid-output", "pending-dead-letter"],
)
@pytest.mark.asyncio
async def test_t04_fail_run_triggers_only_on_unknown_class_exhaustion(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    name: str,
    script: list[str],
    pending_dead_letter: bool,
    outcome_class: str,
    model_outcome: str,
) -> None:
    TARGET.script[:] = script
    run_id = await submit(seeded, tmp_path, name)
    if pending_dead_letter:
        await drain(runtime, ["advance_run"])
        exhaust_tool_job(seeded, run_id)
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", outcome_class)
    assert [outcome for outcome, _ in tool_results(runtime)] == [model_outcome]
    assert run_status(seeded, run_id) == ("succeeded", f"{name} done")
    assert TARGET.sends == (0 if pending_dead_letter else 1)


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
@pytest.mark.asyncio
async def test_t04_an_active_stop_wins_over_fail_run(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 30.0)
    TARGET.script[:] = ["error"] * 3
    run_id = await submit(seeded, tmp_path, "strict_only")
    await drain(runtime)
    assert TARGET.sends == 1  # the retry waits for its backoff
    if stop == "cancel":
        await cancel(seeded, tmp_path, run_id)
    else:
        sql(
            seeded,
            "UPDATE tether.runs SET deadline_at = clock_timestamp() WHERE id = %s RETURNING id",
            run_id,
        )
    release_retries(seeded, run_id)
    await drain(runtime)
    assert TARGET.sends == 1  # no re-send once the stop is visible (ADR-0008)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "unknown")
    status, summary = run_status(seeded, run_id)
    assert status == ("cancelled" if stop == "cancel" else "timed_out")
    assert summary.endswith("tool_exhausted:cas_get_strict")


@pytest.mark.asyncio
async def test_t04_fail_run_is_held_while_another_entry_is_unresolved(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """ADR-0018: external truth first. The run is held, then fails once the entry resolves."""
    TARGET.script[:] = ["error"] * 3
    run_id = await submit(seeded, tmp_path, "strict_only")
    other = seed_unresolved(seeded, run_id, "dispatched")
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    assert "RUN_FINISHED" not in events(seeded, run_id)
    add_wake(seeded, run_id)
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    # The other entry's own outcome arrives (TX7 with its wake).
    sql(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'completed', outcome_class = 'applied'"
        " WHERE id = %s RETURNING id",
        other,
    )
    add_wake(seeded, run_id)
    await drain(runtime)
    assert run_status(seeded, run_id) == ("failed", "tool_exhausted:cas_get_strict")
    assert len(provider_of(runtime).calls) == 1


@pytest.mark.asyncio
async def test_t04_fail_run_after_reconciliation_names_the_reconciled_outcome(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["error"] * 3
    run_id = await submit(seeded, tmp_path, "strict_only")
    other = seed_unresolved(seeded, run_id, "indeterminate")
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    await reconcile(seeded, tmp_path, other, "applied")
    await drain(runtime)
    assert run_status(seeded, run_id) == (
        "failed",
        "tool_exhausted:cas_get_strict; cas_set reconciled_applied",
    )
    assert "RECONCILED" in events(seeded, run_id)
    assert await chain_valid(seeded, tmp_path, run_id)


# --- T-21: preconditions ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t21_a_matching_precondition_applies_the_change(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "set")
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("completed", "applied")
    assert TARGET.store["cfg"] == (8, "b")
    assert TARGET.applies == 1
    assert [outcome for outcome, _ in tool_results(runtime)] == ["applied"]
    authorized = payload(seeded, run_id, "authorized:0")
    assert authorized["preconditions"] == {"expected_version": 7}


@pytest.mark.asyncio
async def test_t21_a_stale_precondition_is_not_applied_and_changes_nothing(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "set_stale")
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class, entry.attempts) == ("failed", "not_applied", 1)
    assert entry.outcome_reason == "not applied: precondition failed: version is 7"
    assert TARGET.store["cfg"] == (7, "a")
    assert (TARGET.sends, TARGET.applies) == (1, 0)
    [(outcome, content)] = tool_results(runtime)
    assert outcome == "not_applied"
    assert "precondition failed" in content
    assert run_status(seeded, run_id) == ("succeeded", "set_stale done")


@pytest.mark.asyncio
async def test_t21_a_re_proposal_after_not_applied_is_a_new_ordinal_key_and_approval(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "gated_retry")
    await drain(runtime)
    [first] = approvals(seeded, run_id)
    await approve(seeded, tmp_path, first[0])
    await drain(runtime)
    found = approvals(seeded, run_id)
    assert len(found) == 2
    await approve(seeded, tmp_path, found[1][0])
    await drain(runtime)
    stale, fresh = ledger(seeded, run_id)
    assert (stale.step_ordinal, stale.outcome_class) == (0, "not_applied")
    assert (fresh.step_ordinal, fresh.outcome_class) == (1, "applied")
    assert stale.idempotency_key != fresh.idempotency_key
    assert stale.call_hash != fresh.call_hash
    assert [a[1] for a in found] == [0, 1]
    assert found[0][2] != found[1][2]
    assert TARGET.store["cfg"] == (8, "b")
    assert TARGET.applies == 1
    assert run_status(seeded, run_id) == ("succeeded", "gated_retry done")


@pytest.mark.asyncio
async def test_t21_the_approval_binds_masked_preconditions_and_the_real_values_execute(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "gated")
    await drain(runtime)
    [(approval_id, _, call_hash, version, arguments, preconditions)] = approvals(seeded, run_id)
    assert version == "1.0.0"
    assert preconditions == {"expected_version": 7, "expected_etag": MASK}
    assert arguments["expected_etag"] == MASK
    requested = payload(seeded, run_id, "approval_requested:0")
    assert requested["preconditions"] == {"expected_version": 7, "expected_etag": MASK}
    # The approval is bound to the exact expected values: another one is another hash.
    run = run_row(seeded, run_id)
    assert call_hash == call_hash_for(runtime.keys, run, GATED_V1, GatedIn(**GATED_7))
    assert call_hash != call_hash_for(runtime.keys, run, GATED_V1, GatedIn(**GATED_6))
    other_etag = GatedIn(**{**GATED_7, "expected_etag": "other"})
    assert call_hash != call_hash_for(runtime.keys, run, GATED_V1, other_etag)
    await approve(seeded, tmp_path, approval_id)
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.call_hash, entry.outcome_class) == (call_hash, "applied")
    assert TARGET.seen_etags == [ETAG]  # the handler got the real value
    assert payload(seeded, run_id, "authorized:0")["preconditions"] == {
        "expected_version": 7,
        "expected_etag": MASK,
    }
    stored = sql(
        seeded,
        "SELECT payload_masked::text FROM tether.audit_events WHERE run_id = %s"
        " UNION ALL SELECT arguments_masked::text || preconditions_masked::text"
        " FROM tether.approvals WHERE run_id = %s",
        run_id,
        run_id,
    )
    assert not [text for (text,) in stored if ETAG in text]
    assert run_status(seeded, run_id) == ("succeeded", "gated done")


@pytest.mark.asyncio
async def test_t21_an_error_while_the_target_checks_is_unknown_never_not_applied(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["error"] * 3
    run_id = await submit(seeded, tmp_path, "set")
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class, entry.attempts) == ("indeterminate", "unknown", 3)
    assert set(TARGET.keys) == {entry.idempotency_key}  # every re-send used the same key
    assert TARGET.store["cfg"] == (7, "a")
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_t21_a_stop_before_dispatch_sends_nothing(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "set")
    await drain(runtime, ["advance_run"])
    assert [e.state for e in ledger(seeded, run_id)] == ["pending"]
    await cancel(seeded, tmp_path, run_id)
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "not_applied")
    assert entry.outcome_reason == "not applied: run stopped before dispatch"
    assert TARGET.sends == 0
    assert TARGET.store["cfg"] == (7, "a")
    assert run_status(seeded, run_id)[0] == "cancelled"


@pytest.mark.asyncio
async def test_t21_a_stop_while_dispatched_records_the_targets_rejection(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "set_stale")

    async def cancel_while_in_flight() -> None:
        TARGET.hook = None
        await cancel(seeded, tmp_path, run_id)

    TARGET.hook = cancel_while_in_flight
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "not_applied")
    assert TARGET.store["cfg"] == (7, "a")
    assert run_status(seeded, run_id)[0] == "cancelled"
    assert len(provider_of(runtime).calls) == 1


# --- timeouts (per send attempt, always unknown) ------------------------------------------------


@pytest.mark.asyncio
async def test_w3_2_a_read_timing_out_on_every_attempt_is_unknown_and_retried_to_its_budget(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["slow"] * 3
    run_id = await submit(seeded, tmp_path, "slow_get")
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class, entry.attempts) == ("failed", "unknown", 3)
    assert entry.outcome_reason == "outcome unknown: TimeoutError"
    assert [outcome for outcome, _ in tool_results(runtime)] == ["failed"]


@pytest.mark.asyncio
async def test_w3_2_an_idempotent_write_timing_out_is_re_sent_with_the_same_key(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["slow", "ok"]
    run_id = await submit(seeded, tmp_path, "set")
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class, entry.attempts) == ("completed", "applied", 2)
    assert TARGET.keys == [entry.idempotency_key] * 2
    assert TARGET.applies == 1
    assert "REDISPATCHED" in events(seeded, run_id)


@pytest.mark.asyncio
async def test_w3_2_a_non_idempotent_write_timing_out_is_indeterminate_and_never_re_sent(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.script[:] = ["slow", "ok"]
    run_id = await submit(seeded, tmp_path, "put")
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class, entry.attempts) == ("indeterminate", "unknown", 1)
    assert TARGET.sends == 1
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


@pytest.mark.parametrize(
    ("timeout", "retained"),
    [(59.9, False), (60.0, False), (120.0, False), (60.0, True)],
    ids=["below-lease", "equal-to-lease", "above-lease", "retained-at-lease"],
)
@pytest.mark.asyncio
async def test_w6_1_worker_startup_accepts_a_tool_timeout_at_or_above_the_lease(
    seeded: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: float,
    retained: bool,
) -> None:
    """Lease heartbeats keep a long send's lease alive (ADR-0021 rule 6), so no bound applies."""
    if retained:
        old = _tool("cas_get", "read", _get, KeyIn, version="0.9.0", timeout_seconds=timeout)
        tools = workload(*OTHERS, GATED_V1, retained=(old,))
    else:
        slow = _tool("cas_get", "read", _get, KeyIn, timeout_seconds=timeout)
        tools = workload(slow, *OTHERS[1:], GATED_V1)
    monkeypatch.setattr(driver_module, "load_workload", lambda name: tools)
    settings = _settings(seeded.db, tmp_path, worker_lease_seconds=60.0)
    runtime = await WorkerRuntime.open(settings, bootstrap=BOOT)
    await runtime.close()


# --- pinned versions (ADR-0011, ADR-0021 rules 12-15) -------------------------------------------


@pytest.mark.asyncio
async def test_w3_2_a_retained_pinned_version_executes_after_a_newer_one_is_offered(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """The newer version requires a permission nobody holds: TX5 evaluates the pinned one."""
    run_id = await submit(seeded, tmp_path, "gated")
    await drain(runtime)
    [(approval_id, *_)] = approvals(seeded, run_id)
    use(runtime, workload(*OTHERS, gated("2.0.0", ("cas:admin",)), retained=(GATED_V1,)))
    await approve(seeded, tmp_path, approval_id)
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert (entry.tool_version, entry.outcome_class) == ("1.0.0", "applied")
    assert TARGET.executed_versions == ["1.0.0"]
    assert "REPLAY_DIVERGENCE" not in events(seeded, run_id)
    assert run_status(seeded, run_id) == ("succeeded", "gated done")


@pytest.mark.asyncio
async def test_w3_2_a_pinned_version_gone_before_the_ledger_is_not_applied_without_substitution(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "gated")
    await drain(runtime)
    [(approval_id, *_)] = approvals(seeded, run_id)
    use(runtime, workload(*OTHERS, gated("2.0.0")))  # 1.0.0 is no longer resolvable
    await approve(seeded, tmp_path, approval_id)
    await drain(runtime)
    assert ledger(seeded, run_id) == []
    assert TARGET.sends == 0
    assert TARGET.executed_versions == []  # 2.0.0 never ran in its place
    assert tool_results(runtime) == [("not_applied", "tool version unavailable")]
    assert "REPLAY_DIVERGENCE" not in events(seeded, run_id)
    assert run_status(seeded, run_id) == ("succeeded", "gated done")


@pytest.mark.asyncio
async def test_w3_2_the_worker_never_sends_a_pending_entry_whose_pinned_version_is_gone(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "gated")
    await drain(runtime)
    [(approval_id, *_)] = approvals(seeded, run_id)
    await approve(seeded, tmp_path, approval_id)
    await drain(runtime, ["advance_run"])
    assert [e.state for e in ledger(seeded, run_id)] == ["pending"]
    use(runtime, workload(*OTHERS, gated("2.0.0")))
    await drain(runtime, ["execute_tool"])
    [entry] = ledger(seeded, run_id)
    assert (entry.state, entry.outcome_class) == ("failed", "not_applied")
    assert entry.outcome_reason == "not applied: tool version not resolvable"
    assert TARGET.sends == 0
    assert TARGET.executed_versions == []


@pytest.mark.asyncio
async def test_w3_2_a_new_turn_uses_the_newly_offered_version(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    use(runtime, workload(*OTHERS, gated("2.0.0"), retained=(GATED_V1,)))
    run_id = await submit(seeded, tmp_path, "gated")
    await drain(runtime)
    [(approval_id, _, _, version, *_)] = approvals(seeded, run_id)
    assert version == "2.0.0"
    await approve(seeded, tmp_path, approval_id)
    await drain(runtime)
    assert TARGET.executed_versions == ["2.0.0"]


# --- outcome classification (migration 0004) ----------------------------------------------------


@pytest.mark.asyncio
async def test_w3_2_an_unclassified_historical_entry_keeps_the_baseline_model_mapping(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "strict_missing")
    await drain(runtime, ["advance_run"])
    await drain(runtime, ["execute_tool"])
    sql(
        seeded,
        "UPDATE tether.ledger_entries SET outcome_class = NULL WHERE run_id = %s RETURNING id",
        run_id,
    )
    await drain(runtime)
    assert [outcome for outcome, _ in tool_results(runtime)] == ["failed"]


@pytest.mark.parametrize("value", ["applied", "not_applied", "unknown", "failed", None])
def test_w3_2_outcome_classes_are_the_closed_set_or_unclassified(
    seeded: Any, rows: Any, value: str | None
) -> None:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        rows.insert(
            conn,
            "ledger_entries",
            rows.child("ledger_entries", "ws-a", seeded.run_a, outcome_class=value),
        )


@pytest.mark.parametrize("value", ["maybe", "APPLIED", "indeterminate", ""])
def test_w3_2_any_other_outcome_class_is_refused(seeded: Any, rows: Any, value: str) -> None:
    with (
        psycopg.connect(seeded.db.migrator_dsn) as conn,
        pytest.raises(errors.CheckViolation),
    ):
        rows.insert(
            conn,
            "ledger_entries",
            rows.child("ledger_entries", "ws-a", seeded.run_a, outcome_class=value),
        )


def _alembic(dsn: str) -> Config:
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    config.attributes["database_url"] = dsn
    return config


def test_w3_2_migration_0004_leaves_historical_entries_unclassified(seeded: Any, rows: Any) -> None:
    command.downgrade(_alembic(seeded.db.migrator_dsn), "0003")
    entry = rows.child("ledger_entries", "ws-a", seeded.run_a, state="failed")
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        rows.insert(conn, "ledger_entries", entry)
    upgrade_to_head(seeded.db.migrator_dsn)
    found = sql(
        seeded, "SELECT outcome_class FROM tether.ledger_entries WHERE id = %s", entry["id"]
    )
    assert found == [(None,)]
