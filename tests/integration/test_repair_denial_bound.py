"""T-22: the repair and denial bound, and malformed tool calls (W3.4, ADR-0016 rule 4, PD-15).

A model that keeps proposing invalid calls (unknown tool, schema-invalid or unparseable
arguments) or calls that policy denies is stopped at a small explicit bound: ``max_repairs`` and
``max_denials`` (PD-15: 3 each). The occurrence that would exceed a budget ends the run ``failed``
with ``repair_budget_exhausted`` or ``denial_budget_exhausted``. That ending is a terminating
result (ADR-0012 rule 6): no further model call, no ``CALL_SKIPPED`` for the rest of the turn,
held while any ledger entry is unresolved (ADR-0018), and an active stop still decides
``cancelled``/``timed_out`` (ADR-0006). Human rejections and expired approvals are not counted.

A tool call whose arguments the adapter could not parse reaches the graph as a malformed
proposal and gets a typed ``invalid`` result that counts as a repair. Its raw text is never
persisted, logged or audited (ADR-0017).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import httpx2
import psycopg
import pytest
import pytest_asyncio
import yaml
from pydantic import SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from tests.contract.providers.harness import openai_provider
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS, WS_APPROVE, AGENT = "ws-bound", "ws-bound-approve", "agent-b"
PERMISSIONS = ["service:read", "service:restart"]


def _workspace(workspace: str, restart: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": workspace,
        "agents": [{"id": AGENT, "workload": "incident_remediation"}],
        "policy": {
            "rules": [{"tool": "get_service_health", "decision": "allow"}, restart],
            "reconciliation": {"reconciler_roles": ["reconciler"]},
            "role_permissions": {"operator": PERMISSIONS},
            "agent_permissions": {AGENT: PERMISSIONS},
        },
    }


BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            _workspace(WS, {"tool": "restart_service", "decision": "deny"}),
            _workspace(
                WS_APPROVE,
                {
                    "tool": "restart_service",
                    "decision": "require_approval",
                    "approver_roles": ["approver"],
                },
            ),
        ]
    }
)

UNKNOWN = "{tool: no_such_tool, arguments: {}}"
SCHEMA_INVALID = "{tool: get_service_health, arguments: {}}"
MALFORMED = "{tool: get_service_health, malformed: true}"
DENIED = "{tool: restart_service, arguments: {service: checkout}}"
READ = "{tool: get_service_health, arguments: {service: checkout}}"


def _scenario(name: str, *turns: str) -> str:
    lines = [f"name: {name}", "turns:"]
    lines += [f"  - tool_calls: [{turn}]" for turn in turns]
    lines.append("  - final: done")
    return "\n".join(lines) + "\n"


SCENARIOS = {
    "invalid3": _scenario("invalid3", UNKNOWN, SCHEMA_INVALID, UNKNOWN),
    "invalid4": _scenario("invalid4", UNKNOWN, SCHEMA_INVALID, UNKNOWN, SCHEMA_INVALID),
    "deny3": _scenario("deny3", DENIED, DENIED, DENIED),
    "deny4": _scenario("deny4", DENIED, DENIED, DENIED, DENIED),
    "mixed": _scenario("mixed", UNKNOWN, DENIED, SCHEMA_INVALID, DENIED, UNKNOWN, DENIED),
    "reject4": _scenario("reject4", DENIED, DENIED, DENIED, DENIED),
    "malformed4": _scenario("malformed4", MALFORMED, MALFORMED, MALFORMED, MALFORMED),
    # The fourth repair is the first call of a two-call turn: the read is never governed.
    "invalid_multi": _scenario("invalid_multi", UNKNOWN, UNKNOWN, UNKNOWN, f"{UNKNOWN}, {READ}"),
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
def demo_target() -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    yield
    demo_client.set_transport(None)


def _settings(db: Any, tmp_path: Path, **overrides: Any) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
        worker_poll_interval_seconds=0.05,
        **overrides,
    )


Opener = Callable[..., Any]


@pytest_asyncio.fixture
async def open_runtime(seeded: Any, tmp_path: Path) -> AsyncIterator[Opener]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    opened: list[WorkerRuntime] = []

    async def _open(provider: Any = None, **overrides: Any) -> WorkerRuntime:
        runtime = await WorkerRuntime.open(
            _settings(seeded.db, tmp_path, **overrides),
            bootstrap=BOOT,
            provider=provider if provider is not None else RecordingProvider(tmp_path),
        )
        opened.append(runtime)
        return runtime

    try:
        yield _open
    finally:
        for runtime in opened:
            await runtime.close()


async def call_api(seeded: Any, tmp_path: Path, method: str, url: str, **kw: Any) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kw)


async def submit(seeded: Any, tmp_path: Path, message: str, workspace: str = WS) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": workspace,
            "agent_id": AGENT,
            "requester": {"sub": "alice", "roles": ["operator"]},
        },
        "task_input": {"message": message},
    }
    response = await call_api(seeded, tmp_path, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(80):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_row(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return rows(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event for (event,) in found]


def dedupe_keys(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(seeded, "SELECT dedupe_key FROM tether.audit_events WHERE run_id = %s", run_id)
    return [key for (key,) in found]


def model_calls(seeded: Any, run_id: uuid.UUID) -> int:
    found = rows(seeded, "SELECT count(*) FROM tether.model_calls WHERE run_id = %s", run_id)
    return int(found[0][0])


def seed_unresolved(seeded: Any, run_id: uuid.UUID, workspace: str = WS) -> uuid.UUID:
    """Another entry of the same run whose real-world outcome is not known yet."""
    entry_id = uuid.uuid4()
    rows(
        seeded,
        "INSERT INTO tether.ledger_entries (id, workspace_id, run_id, step_ordinal, tool,"
        " tool_version, effect, idempotency_support, call_hash, idempotency_key, state, attempts)"
        " VALUES (%s, %s, %s, 99, 'restart_service', '1.0.0', 'write', 'idempotent', %s, %s,"
        " 'dispatched', 1) RETURNING id",
        entry_id,
        workspace,
        run_id,
        "c" * 64,
        "tth_" + "c" * 52,
    )
    return entry_id


def add_wake(seeded: Any, run_id: uuid.UUID, workspace: str = WS) -> None:
    rows(
        seeded,
        "INSERT INTO tether.jobs (id, kind, workspace_id, run_id, max_attempts)"
        " VALUES (%s, 'advance_run', %s, %s, 8) RETURNING id",
        uuid.uuid4(),
        workspace,
        run_id,
    )


def outcomes(provider: RecordingProvider) -> list[str | None]:
    """The neutral outcome of every tool result the model saw, in order."""
    last = provider.calls[-1]
    return [m.outcome.value if m.outcome else None for m in last if m.role == "tool"]


# --- the bound ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t22_four_invalid_proposals_end_the_run_with_repair_budget_exhausted(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=invalid4")
    await drain(runtime)
    assert run_row(seeded, run_id) == ("failed", "repair_budget_exhausted")
    assert events(seeded, run_id).count("CALL_INVALID") == 4
    assert events(seeded, run_id)[-1] == "RUN_FINISHED"
    assert model_calls(seeded, run_id) == 4  # no model call after the bound
    await drain(runtime)
    assert run_row(seeded, run_id) == ("failed", "repair_budget_exhausted")


@pytest.mark.asyncio
async def test_t22_three_invalid_proposals_do_not_end_the_run(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=invalid3")
    await drain(runtime)
    assert run_row(seeded, run_id) == ("succeeded", "done")
    assert events(seeded, run_id).count("CALL_INVALID") == 3


@pytest.mark.asyncio
async def test_t22_four_denials_end_the_run_with_denial_budget_exhausted(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=deny4")
    await drain(runtime)
    assert run_row(seeded, run_id) == ("failed", "denial_budget_exhausted")
    assert model_calls(seeded, run_id) == 4
    assert not rows(seeded, "SELECT 1 FROM tether.ledger_entries WHERE run_id = %s", run_id)


@pytest.mark.asyncio
async def test_t22_three_denials_do_not_end_the_run(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=deny3")
    await drain(runtime)
    assert run_row(seeded, run_id) == ("succeeded", "done")


@pytest.mark.asyncio
async def test_t22_the_two_budgets_are_counted_separately(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=mixed")
    await drain(runtime)
    assert run_row(seeded, run_id) == ("succeeded", "done")


@pytest.mark.asyncio
async def test_t22_the_budgets_are_settings(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    runtime = await open_runtime(max_repairs=2, max_denials=2)
    repaired = await submit(seeded, tmp_path, "go #scenario=invalid3")
    denied = await submit(seeded, tmp_path, "go #scenario=deny3")
    await drain(runtime)
    assert run_row(seeded, repaired) == ("failed", "repair_budget_exhausted")
    assert run_row(seeded, denied) == ("failed", "denial_budget_exhausted")


def test_t22_the_budgets_default_to_pd15_and_must_be_positive() -> None:
    settings = Settings(database_url=SecretStr("postgresql://x"), env="test")
    assert (settings.max_repairs, settings.max_denials) == (3, 3)
    for field in ("max_repairs", "max_denials"):
        with pytest.raises(ValueError, match=field):
            Settings(database_url=SecretStr("postgresql://x"), env="test", **{field: 0})


@pytest.mark.asyncio
async def test_t22_human_rejections_are_not_counted(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=reject4", workspace=WS_APPROVE)
    for _ in range(4):
        await drain(runtime)
        [(approval_id,)] = rows(
            seeded,
            "SELECT id FROM tether.approvals WHERE run_id = %s AND status = 'pending'",
            run_id,
        )
        response = await call_api(
            seeded,
            tmp_path,
            "POST",
            f"/v1/approvals/{approval_id}/reject",
            json={
                "dev_identity": {"workspace_id": WS_APPROVE, "sub": "bob", "roles": ["approver"]},
                "reason": "not now",
            },
        )
        assert response.status_code == 200, response.text
    await drain(runtime)
    assert run_row(seeded, run_id) == ("succeeded", "done")


@pytest.mark.asyncio
async def test_t22_the_bound_is_terminating_no_skip_events_and_no_later_governance(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    """ADR-0012 rule 6: the rest of the turn is neither governed nor marked skipped."""
    provider = RecordingProvider(tmp_path)
    runtime = await open_runtime(provider)
    run_id = await submit(seeded, tmp_path, "go #scenario=invalid_multi")
    await drain(runtime)
    assert run_row(seeded, run_id) == ("failed", "repair_budget_exhausted")
    keys = dedupe_keys(seeded, run_id)
    assert "decision:3" in keys
    assert "decision:4" not in keys
    assert not [key for key in keys if key.startswith("skipped:")]
    assert "CALL_SKIPPED" not in events(seeded, run_id)
    assert len(provider.calls) == 4


@pytest.mark.asyncio
async def test_t22_the_bound_with_a_write_in_flight_holds_the_run_until_it_resolves(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    """ADR-0018: external truth first."""
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=invalid4")
    other = seed_unresolved(seeded, run_id)
    await drain(runtime)
    assert run_row(seeded, run_id)[0] == "awaiting_reconciliation"
    assert "RUN_FINISHED" not in events(seeded, run_id)
    add_wake(seeded, run_id)
    await drain(runtime)
    assert run_row(seeded, run_id)[0] == "awaiting_reconciliation"
    rows(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'completed', outcome_class = 'applied'"
        " WHERE id = %s RETURNING id",
        other,
    )
    add_wake(seeded, run_id)
    await drain(runtime)
    assert run_row(seeded, run_id) == ("failed", "repair_budget_exhausted")
    assert model_calls(seeded, run_id) == 4


@pytest.mark.asyncio
async def test_t22_an_active_stop_decides_the_status_at_the_bound(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    """ADR-0006: the stop keeps precedence; the bound is named in the summary."""
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=deny4")
    other = seed_unresolved(seeded, run_id)
    await drain(runtime)
    assert run_row(seeded, run_id)[0] == "awaiting_reconciliation"
    cancelled = await call_api(
        seeded,
        tmp_path,
        "POST",
        f"/v1/tasks/runs/{run_id}/cancel",
        json={"dev_identity": {"workspace_id": WS, "sub": "alice", "roles": []}},
    )
    assert cancelled.status_code == 202, cancelled.text
    rows(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'completed', outcome_class = 'applied'"
        " WHERE id = %s RETURNING id",
        other,
    )
    add_wake(seeded, run_id)
    await drain(runtime)
    status, summary = run_row(seeded, run_id)
    assert status == "cancelled"
    assert summary.startswith("cancelled by alice")
    assert summary.endswith("denial_budget_exhausted")


# --- malformed tool calls ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t22_four_malformed_calls_exhaust_the_repair_budget(
    open_runtime: Opener, seeded: Any, tmp_path: Path
) -> None:
    runtime = await open_runtime()
    run_id = await submit(seeded, tmp_path, "go #scenario=malformed4")
    await drain(runtime)
    assert run_row(seeded, run_id) == ("failed", "repair_budget_exhausted")
    assert events(seeded, run_id).count("CALL_INVALID") == 4


RAW = '{"service": "checkout", "note": "hunter2-raw-model-text"'  # not valid JSON
NEEDLE = "hunter2-raw-model-text"


def _malformed_then_final(request: httpx2.Request) -> httpx2.Response:
    payload = json.loads(request.content)
    answered = any(item.get("type") == "function_call_output" for item in payload["input"])
    if answered:
        output: list[dict[str, Any]] = [
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "gave up"}],
            }
        ]
    else:
        output = [
            {
                "id": "fc_0",
                "type": "function_call",
                "call_id": "call_0",
                "name": "get_service_health",
                "arguments": RAW,
            }
        ]
    body = {"id": "resp", "output": output, "usage": {"input_tokens": 5, "output_tokens": 2}}
    return httpx2.Response(200, json=body)


@pytest.mark.asyncio
async def test_t22_a_malformed_call_from_the_adapter_is_a_typed_invalid_result_never_persisted(
    open_runtime: Opener,
    seeded: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    runtime = await open_runtime(openai_provider(_malformed_then_final))
    run_id = await submit(seeded, tmp_path, "check checkout")
    await drain(runtime)
    # The run continued: the malformed call was a typed invalid result, not a provider failure.
    assert run_row(seeded, run_id) == ("succeeded", "gave up")
    [(payload,)] = rows(
        seeded,
        "SELECT payload_masked FROM tether.audit_events WHERE run_id = %s"
        " AND event_type = 'CALL_INVALID'",
        run_id,
    )
    assert payload["decision"] == "invalid"
    assert payload["reasons"] == ["arguments: malformed"]
    [(status, error_code)] = rows(
        seeded,
        "SELECT status, error_code FROM tether.model_calls WHERE run_id = %s"
        " ORDER BY turn_ordinal LIMIT 1",
        run_id,
    )
    assert (status, error_code) == ("succeeded", None)
    # The raw text appears in no row of any Tether or LangGraph table, as text or as bytes.
    hexed = NEEDLE.encode().hex()
    for schema in ("tether", "langgraph"):
        tables = rows(
            seeded,
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            schema,
        )
        for (table,) in tables:
            found = rows(
                seeded,
                f"SELECT count(*) FROM {schema}.{table} t"  # noqa: S608 - catalog names
                " WHERE t::text LIKE %s OR t::text LIKE %s",
                f"%{NEEDLE}%",
                f"%{hexed}%",
            )
            assert found[0][0] == 0, f"raw model text persisted in {schema}.{table}"
    captured = capsys.readouterr()
    assert NEEDLE not in caplog.text
    assert NEEDLE not in captured.out + captured.err
