"""R1 live: one governed incident-remediation run end to end on the real OpenAI API (T-11).

Marker ``live``; manual dispatch only, never in ordinary CI. Opt in with::

    TETHER_MODEL_PROVIDER=openai
    TETHER_OPENAI_API_KEY=<the operator's key>   # read through SecretsProvider, never logged
    TETHER_OPENAI_MODEL=<an inexpensive model>   # no default: nobody spends on an unchosen model

    uv run poe test-live

The test skips unless all three are set *and* the run selected the ``live`` marker, so a key
left in ``deploy/compose/.env`` can never make ``poe test-integration`` spend money.

The worker builds its provider from configuration exactly as in production. The run reads the
service, asks to restart it, is approved by a second identity, and finishes. Only structural
facts are asserted, never model wording: a terminal status; every executed call has its
decision, authorization, dispatch and outcome events; the audit chain verifies; every
``model_calls`` row is OpenAI's with reported tokens. Only a synthetic task and the reference
workload's tool manifest are sent.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
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
from tether.providers.openai_provider import OpenAIProvider
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.worker.main import process_next_job

KEY_ENV, MODEL_ENV, PROVIDER_ENV = (
    "TETHER_OPENAI_API_KEY",
    "TETHER_OPENAI_MODEL",
    "TETHER_MODEL_PROVIDER",
)
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "abandoned"}
WS, AGENT = "ws-live", "agent-live"
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": AGENT, "workload": "incident_remediation"}],
                "policy": {
                    "rules": [
                        {"tool": "get_service_health", "decision": "allow"},
                        {
                            "tool": "restart_service",
                            "decision": "require_approval",
                            "approver_roles": ["approver"],
                        },
                    ],
                    "role_permissions": {"operator": ["service:read", "service:restart"]},
                    "agent_permissions": {AGENT: ["service:read", "service:restart"]},
                },
            }
        ]
    }
)
TASK = (
    "The checkout service is reported unhealthy. Check its health with the tools, restart it if "
    "it is unhealthy, then confirm in one sentence what you did."
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (os.environ.get(KEY_ENV) and os.environ.get(MODEL_ENV)),
        reason=f"set {KEY_ENV} and {MODEL_ENV} to run the live governed run",
    ),
]


@pytest.fixture(autouse=True)
def explicitly_live(request: pytest.FixtureRequest) -> None:
    """Paid calls only when the run asked for live tests by marker (`poe test-live`)."""
    if "live" not in str(request.config.getoption("-m") or ""):
        pytest.skip("live test: select it with -m live (uv run poe test-live)")
    if os.environ.get(PROVIDER_ENV) != "openai":
        pytest.skip(f"set {PROVIDER_ENV}=openai to run the live governed run")


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    yield
    demo_client.set_transport(None)


def _settings(db: Any, tmp_path: Path) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    # The provider, model and key come from the environment, as in production.
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        worker_poll_interval_seconds=0.05,
    )


@pytest_asyncio.fixture
async def runtime(seeded: Any, tmp_path: Path) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    worker = await WorkerRuntime.open(_settings(seeded.db, tmp_path), bootstrap=BOOT)
    try:
        yield worker
    finally:
        await worker.close()


async def call_api(seeded: Any, tmp_path: Path, method: str, url: str, **kw: Any) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kw)


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


async def test_r1_live_a_governed_run_completes_on_the_real_model(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    assert isinstance(runtime.provider, OpenAIProvider), "configuration did not select OpenAI"
    submitted = await call_api(
        seeded,
        tmp_path,
        "POST",
        "/v1/tasks/runs",
        json={
            "dev_identity": {
                "workspace_id": WS,
                "agent_id": AGENT,
                "requester": {"sub": "alice", "roles": ["operator"]},
            },
            "task_input": {"message": TASK},
        },
    )
    assert submitted.status_code == 201, submitted.text
    run_id = uuid.UUID(submitted.json()["run_id"])

    approved: set[Any] = set()
    status = ""
    for _ in range(20):
        while await process_next_job(runtime):
            pass
        [(status,)] = rows(seeded, "SELECT status FROM tether.runs WHERE id = %s", run_id)
        if status in TERMINAL:
            break
        assert status == "awaiting_approval", f"run stuck in {status}"
        pending = rows(
            seeded,
            "SELECT id FROM tether.approvals WHERE run_id = %s AND status = 'pending'",
            run_id,
        )
        for (approval_id,) in pending:
            assert approval_id not in approved
            approved.add(approval_id)
            decided = await call_api(
                seeded,
                tmp_path,
                "POST",
                f"/v1/approvals/{approval_id}/approve",
                json={
                    "dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]},
                    "reason": "live proof",
                },
            )
            assert decided.status_code == 200, decided.text
    assert status in TERMINAL

    keys: dict[str, str] = dict(
        rows(
            seeded,
            "SELECT dedupe_key, event_type FROM tether.audit_events WHERE run_id = %s",
            run_id,
        )
    )
    entries = rows(
        seeded,
        "SELECT id, step_ordinal, state FROM tether.ledger_entries WHERE run_id = %s",
        run_id,
    )
    for entry_id, ordinal, state in entries:
        assert keys[f"decision:{ordinal}"] == "POLICY_DECISION"
        assert keys[f"authorized:{ordinal}"] == "EXECUTION_AUTHORIZED"
        assert [k for k in keys if k.startswith(f"dispatched:{entry_id}:")]
        assert keys[f"outcome:{entry_id}"] == "TOOL_OUTCOME"
        assert state in ("completed", "failed")
    assert keys["run_finished"] == "RUN_FINISHED"

    view = await call_api(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    assert view.status_code == 200, view.text
    assert view.json()["audit_chain_valid"] is True

    calls = rows(
        seeded,
        "SELECT provider, status, input_tokens, output_tokens FROM tether.model_calls"
        " WHERE run_id = %s",
        run_id,
    )
    assert calls
    for provider, call_status, input_tokens, output_tokens in calls:
        assert provider == "openai"
        if call_status == "succeeded":
            assert input_tokens is not None
            assert output_tokens is not None
