"""Shared fixtures for the Lane I evidence tests (P-16, P-17, T-16 run level).

A test-only workload on top of the incident reference workload: ``rotate_secret`` is a high-risk,
approval-gated write whose *argument* (``api_token``) and *output* (``new_token``) both carry a
``Sensitive`` field, so one governed run exercises every masking boundary. The canary values are
synthetic and unique; no real credential appears here.

``RecordingProvider`` is the scripted provider plus a copy of everything the run handed to it,
for the "messages handed to the model provider" sink.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Annotated, Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from pydantic import BaseModel, SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from tests.integration.test_authentication import (
    APP_KEY,
    APPROVALS,
    BOOT,
    OPS,
    SCENARIOS,
    TASKS,
    TEST_WORKLOAD,
    WS,
    headers,
    token,
)
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, Sensitive, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

CANARY_ARG = "canary-arg-7f3a91c2e5d84b60"
CANARY_OUT = "canary-out-b81d4e07a9c35f26"


class RotateIn(BaseModel):
    service: str
    api_token: Annotated[str, Sensitive]


class RotateOut(BaseModel):
    service: str
    status: str
    new_token: Annotated[str, Sensitive]


async def _rotate(args: RotateIn, ctx: ToolContext) -> RotateOut:
    return RotateOut(service=args.service, status="rotated", new_token=CANARY_OUT)


ROTATE = ToolDescriptor(
    name="rotate_secret",
    version="1.0.0",
    description="Test-only write whose argument and output both carry a Sensitive field.",
    input_model=RotateIn,
    output_model=RotateOut,
    effect="write",
    idempotency="supported",
    handler=_rotate,
    risk="high",
    required_permissions=("service:restart",),
)
SENSITIVE_WORKLOAD = Workload(
    name=TEST_WORKLOAD.name,
    tools=(*TEST_WORKLOAD.tools, ROTATE),
    agent=AgentDefinition(system_prompt="test", tools=(*TEST_WORKLOAD.agent.tools, ROTATE.name)),
)


def _boot() -> BootstrapFile:
    data = BOOT.model_dump(mode="json")
    for workspace in data["workspaces"]:
        workspace["policy"]["rules"].append(
            {"tool": "rotate_secret", "decision": "require_approval", "approver_roles": ["oncall"]}
        )
    return BootstrapFile.model_validate(data)


BOOT_SENSITIVE = _boot()
ROTATE_SCENARIO = (
    "name: rotate\nturns:\n"
    "  - tool_calls: [{tool: rotate_secret,"
    f" arguments: {{service: checkout, api_token: {CANARY_ARG}}}}}]\n"
    "  - final: rotated\n"
)


class RecordingProvider(ScriptedProvider):
    """The scripted provider that keeps a JSON copy of every history and manifest it was given."""

    def __init__(self, scenarios_dir: Path) -> None:
        super().__init__(scenarios_dir, env="test")
        self.histories: list[list[dict[str, Any]]] = []
        self.manifests: list[list[dict[str, Any]]] = []

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        self.histories.append([message.model_dump(mode="json") for message in history])
        self.manifests.append([dict(entry) for entry in manifest])
        return await super().complete(history, manifest)


def settings_for(db: Any, tmp_path: Path, config: BootstrapFile = BOOT_SENSITIVE) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


class CountingTransport(httpx.AsyncBaseTransport):
    """The in-process demo target, counting every request that reaches it ("nothing sent")."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._inner = httpx.ASGITransport(app=create_demo_target())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[CountingTransport]:
    transport = CountingTransport()
    demo_client.set_transport(transport)
    yield transport
    demo_client.set_transport(None)


@pytest.fixture
def provider(tmp_path: Path) -> RecordingProvider:
    for name, text in {**SCENARIOS, "rotate": ROTATE_SCENARIO}.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    return RecordingProvider(tmp_path)


@pytest_asyncio.fixture
async def runtime(
    seeded: Any, tmp_path: Path, provider: RecordingProvider
) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT_SENSITIVE)
    runtime = await WorkerRuntime.open(
        settings_for(seeded.db, tmp_path), bootstrap=BOOT_SENSITIVE, provider=provider
    )
    runtime.registries[SENSITIVE_WORKLOAD.name] = ToolRegistry(SENSITIVE_WORKLOAD)
    try:
        yield runtime
    finally:
        await runtime.close()


async def call(
    seeded: Any,
    tmp_path: Path,
    method: str,
    url: str,
    *,
    config: BootstrapFile = BOOT_SENSITIVE,
    **kwargs: Any,
) -> httpx.Response:
    app = create_app(settings_for(seeded.db, tmp_path, config))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(80):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def alice() -> dict[str, str]:
    return headers(token("alice", ["operator"], TASKS), APP_KEY.raw)


def bob() -> dict[str, str]:
    return headers(token("bob", ["approver", "oncall"], APPROVALS))


def ops_admin() -> dict[str, str]:
    return headers(token("olga", ["workspace_admin", "reconciler", "sre"], OPS))


async def submit(runtime: WorkerRuntime, seeded: Any, tmp_path: Path, scenario: str) -> uuid.UUID:
    response = await call(
        seeded,
        tmp_path,
        "POST",
        "/v1/tasks/runs",
        headers=alice(),
        json={"task_input": {"message": f"go #scenario={scenario}"}},
    )
    assert response.status_code == 201, response.text
    run_id = uuid.UUID(response.json()["run_id"])
    await drain(runtime)
    return run_id


def run_status(seeded: Any, run_id: uuid.UUID) -> str:
    return str(rows(seeded, "SELECT status FROM tether.runs WHERE id = %s", run_id)[0][0])


def approval_id(seeded: Any, run_id: uuid.UUID) -> uuid.UUID:
    [(found,)] = rows(seeded, "SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
    assert isinstance(found, uuid.UUID)
    return found


async def approve(seeded: Any, tmp_path: Path, found: uuid.UUID) -> httpx.Response:
    return await call(
        seeded,
        tmp_path,
        "POST",
        f"/v1/approvals/{found}/approve",
        headers=bob(),
        json={"reason": "approved for the evidence test"},
    )


__all__ = [
    "CANARY_ARG",
    "CANARY_OUT",
    "OPS",
    "WS",
    "CountingTransport",
    "RecordingProvider",
    "alice",
    "approval_id",
    "approve",
    "bob",
    "call",
    "demo_target",
    "drain",
    "ops_admin",
    "provider",
    "rows",
    "run_status",
    "runtime",
    "settings_for",
    "submit",
]
