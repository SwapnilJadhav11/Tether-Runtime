"""T-11: the same governed run on two providers, chosen by configuration only (TDR-003, D-5).

The claim under test is narrow and literal: *changing provider configuration changes the model
provider, not Tether's governance or execution logic.* So the same task runs twice — once on the
scripted provider, once on the OpenAI adapter — and the governed artifacts are compared: run
status and summary, the audit event sequence with its policy decision, and the ledger entry.
Only the provider identity and the recorded usage may differ.

Nothing is injected. The worker builds its provider from settings exactly as in production, so
this exercises `build_provider`, the real adapter and real HTTP. The adapter is pointed at a
local stub through `TETHER_OPENAI_BASE_URL`, so the suite never reaches OpenAI and needs no
credential; the live proof against the real API is
`tests/contract/providers/test_T11_live_openai.py`.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from pydantic import SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.worker.main import process_next_job

WS = "ws-swap"
AGENT = "agent-s"
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": AGENT, "workload": "incident_remediation"}],
                "policy": {
                    "rules": [{"tool": "get_service_health", "decision": "allow"}],
                    # ADR-0020: the operator role and the agent must both grant the permission.
                    "role_permissions": {"operator": ["service:read"]},
                    "agent_permissions": {"agent-s": ["service:read"]},
                },
            }
        ]
    }
)

# Two turns: read the service, then answer. The scripted scenario and the stub below are two
# renderings of the same model behaviour.
SCENARIO = (
    "name: swap\nturns:\n"
    "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
    "  - final: checkout is healthy\n"
)
FINAL_TEXT = "checkout is healthy"
# Not a credential: the local stub ignores it. It exists so the request path is exercised.
STUB_KEY = "sk-test-not-a-real-key-000000000000"


# --------------------------------------------------------------------------------------------
# A local stand-in for the OpenAI Responses API
# --------------------------------------------------------------------------------------------


def _tool_call_body() -> dict[str, Any]:
    return {
        "id": "resp_1",
        "output": [
            {
                "id": "fc_0",
                "type": "function_call",
                "call_id": "call_0",
                "name": "get_service_health",
                "arguments": json.dumps({"service": "checkout"}),
            }
        ],
        "usage": {"input_tokens": 310, "output_tokens": 18, "total_tokens": 328},
    }


def _final_body() -> dict[str, Any]:
    return {
        "id": "resp_2",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": FINAL_TEXT}],
            }
        ],
        "usage": {"input_tokens": 402, "output_tokens": 9, "total_tokens": 411},
    }


class _StubState:
    def __init__(self) -> None:
        self.authorization: list[str] = []
        self.payloads: list[dict[str, Any]] = []


def _handler(state: _StubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # The name is BaseHTTPRequestHandler's required spelling.
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            state.payloads.append(payload)
            state.authorization.append(self.headers.get("Authorization", ""))
            # Deterministic like the scripted provider: the answer depends only on whether the
            # conversation already carries a tool result.
            answered = any(
                item.get("type") == "function_call_output" for item in payload.get("input", [])
            )
            body = json.dumps(_final_body() if answered else _tool_call_body()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return  # keep the test output clean

    return Handler


@pytest.fixture
def openai_stub() -> Iterator[tuple[str, _StubState]]:
    state = _StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    yield
    demo_client.set_transport(None)


# --------------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------------


def _settings(db: Any, tmp_path: Path) -> Settings:
    """Settings that name no provider: selection comes from the environment."""
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    (tmp_path / "swap.yaml").write_text(SCENARIO, encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
        worker_poll_interval_seconds=0.05,
    )


@pytest_asyncio.fixture
async def run_task(seeded: Any, tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    opened: list[WorkerRuntime] = []

    async def _run(message: str) -> uuid.UUID:
        settings = _settings(seeded.db, tmp_path)
        app = create_app(settings)
        body = {
            "dev_identity": {
                "workspace_id": WS,
                "agent_id": AGENT,
                "requester": {"sub": "alice", "roles": ["operator"]},
            },
            "task_input": {"message": message},
        }
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
        ):
            response = await http.post("/v1/tasks/runs", json=body)
        assert response.status_code == 201, response.text
        run_id = uuid.UUID(response.json()["run_id"])
        # No `provider=`: the worker builds its provider from configuration, as in production.
        runtime = await WorkerRuntime.open(settings, bootstrap=BOOT)
        opened.append(runtime)
        for _ in range(60):
            if not await process_next_job(runtime):
                break
        else:
            raise AssertionError("the queue did not drain")
        return run_id

    try:
        yield _run
    finally:
        for runtime in opened:
            await runtime.close()


def _rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def governed(seeded: Any, run_id: uuid.UUID) -> dict[str, Any]:
    """Everything about the run that governance owns. The provider must not change any of it."""
    status, summary = _rows(
        seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id
    )[0]
    return {
        "status": status,
        "summary": summary,
        # Event types in order, each with its decision and tool where it has one. Model text and
        # arguments are deliberately not compared: only governance is claimed to be identical.
        "audit": _rows(
            seeded,
            "SELECT event_type, tool, payload_masked->>'decision' FROM tether.audit_events"
            " WHERE run_id = %s ORDER BY seq",
            run_id,
        ),
        "ledger": _rows(
            seeded,
            "SELECT step_ordinal, tool, state, effect, resolution FROM tether.ledger_entries"
            " WHERE run_id = %s ORDER BY step_ordinal",
            run_id,
        ),
    }


def model_calls(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return _rows(
        seeded,
        "SELECT turn_ordinal, provider, model, status, input_tokens, output_tokens, error_code"
        " FROM tether.model_calls WHERE run_id = %s ORDER BY turn_ordinal",
        run_id,
    )


def _configure_openai(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    monkeypatch.setenv("TETHER_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("TETHER_OPENAI_MODEL", "stub-model")
    monkeypatch.setenv("TETHER_OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("TETHER_OPENAI_API_KEY", STUB_KEY)


# --------------------------------------------------------------------------------------------
# The proof
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t11_the_same_governed_run_executes_on_either_provider(
    run_task: Callable[..., Any],
    seeded: Any,
    openai_stub: tuple[str, _StubState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, stub = openai_stub

    # Run A: nothing configured, so the scripted provider is used.
    monkeypatch.delenv("TETHER_MODEL_PROVIDER", raising=False)
    scripted_run = await run_task("check checkout #scenario=swap")

    # Run B: the configuration changes, and nothing else does.
    _configure_openai(monkeypatch, base_url)
    openai_run = await run_task("check checkout")

    scripted = governed(seeded, scripted_run)
    hosted = governed(seeded, openai_run)

    # The runs really did go through different providers.
    assert [row[1] for row in model_calls(seeded, openai_run)] == ["openai", "openai"]
    assert [row[1] for row in model_calls(seeded, scripted_run)] == ["scripted", "scripted"]
    assert stub.payloads, "the hosted run never called the provider"

    # The governed outcome is identical.
    assert hosted["status"] == scripted["status"] == "succeeded"
    assert hosted["summary"] == scripted["summary"] == FINAL_TEXT
    assert hosted["audit"] == scripted["audit"]
    assert hosted["ledger"] == scripted["ledger"]
    # The call really was governed, not waved through.
    assert ("POLICY_DECISION", "get_service_health", "allow") in hosted["audit"]
    assert [(row[1], row[2], row[3]) for row in hosted["ledger"]] == [
        ("get_service_health", "completed", "read")
    ]


@pytest.mark.asyncio
async def test_t11_the_hosted_run_records_the_usage_the_provider_reported(
    run_task: Callable[..., Any],
    seeded: Any,
    openai_stub: tuple[str, _StubState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, _stub = openai_stub
    _configure_openai(monkeypatch, base_url)

    run_id = await run_task("check checkout")
    calls = model_calls(seeded, run_id)
    assert [(row[0], row[3]) for row in calls] == [(0, "succeeded"), (1, "succeeded")]
    # Exactly what the provider reported for each attempt, per turn.
    assert [(row[4], row[5]) for row in calls] == [(310, 18), (402, 9)]
    assert all(row[6] is None for row in calls)


@pytest.mark.asyncio
async def test_t11_the_hosted_run_sends_the_credential_and_the_governed_history(
    run_task: Callable[..., Any],
    seeded: Any,
    openai_stub: tuple[str, _StubState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second turn carries the governed tool result back, paired with its call."""
    base_url, stub = openai_stub
    _configure_openai(monkeypatch, base_url)

    await run_task("check checkout")

    assert stub.authorization
    assert all(header == f"Bearer {STUB_KEY}" for header in stub.authorization)
    assert len(stub.payloads) == 2
    first, second = stub.payloads
    assert first["model"] == "stub-model"
    assert first["parallel_tool_calls"] is False
    assert first["store"] is False
    assert {tool["name"] for tool in first["tools"]} >= {"get_service_health"}
    kinds = [item.get("type") for item in second["input"]]
    assert "function_call" in kinds
    assert "function_call_output" in kinds
    outputs = [item for item in second["input"] if item.get("type") == "function_call_output"]
    assert outputs
    assert outputs[0]["call_id"] == "call_0"
