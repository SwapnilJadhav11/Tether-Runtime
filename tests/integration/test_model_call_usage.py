"""Truthful per-attempt recording in `model_calls` (ADR-0016 rule 5, PRD provider usage).

Every model attempt already gets a row. These tests pin what that row carries: the usage the
provider actually reported, nulls when it reported none, and a normalised error code when the
call failed — and never the provider's own error prose, which can quote the request.

Dollar cost stays deferred (D-7): `cost_usd` is not written here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from psycopg.rows import dict_row
from pydantic import SecretStr

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, ModelUsage, NeutralMessage
from tether.providers.protocol import ProviderCapabilities, ProviderRateLimitedError
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS = "ws-usage"
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": "agent-u", "workload": "incident_remediation"}],
                "policy": {
                    "rules": [{"tool": "get_service_health", "decision": "allow"}],
                    # ADR-0020: the operator role and the agent must both grant the permission.
                    "role_permissions": {"operator": ["service:read"]},
                    "agent_permissions": {"agent-u": ["service:read"]},
                },
            }
        ]
    }
)

# A scenario for the scripted provider, which reports no usage of its own.
SCENARIO = "name: answer\nturns:\n  - final: all good\n"

# Prose a provider might put in an exception. The adapter never copies provider error text into
# a Tether exception, but the authoritative row must not carry it even if one did.
PROSE = "quota exceeded for org-abc: prompt began 'db_password=hunter2'"


class _StubProvider:
    """A provider under the test's control, so the recorded row is a function of the test."""

    name = "stub"
    model = "stub-v1"

    def __init__(self, *, turn: ModelTurn | None = None, error: Exception | None = None) -> None:
        self._turn = turn
        self._error = error

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(tool_calling=True, parallel_tool_calls_controllable=True)

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        if self._error is not None:
            raise self._error
        assert self._turn is not None
        return self._turn


def _settings(db: Any, tmp_path: Path) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
        worker_poll_interval_seconds=0.05,
    )


Runtime = Callable[..., Any]


@pytest_asyncio.fixture
async def runtime_for(seeded: Any, tmp_path: Path) -> AsyncIterator[Runtime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    (tmp_path / "answer.yaml").write_text(SCENARIO, encoding="utf-8")
    opened: list[WorkerRuntime] = []

    async def _open(provider: Any) -> WorkerRuntime:
        runtime = await WorkerRuntime.open(
            _settings(seeded.db, tmp_path), bootstrap=BOOT, provider=provider
        )
        opened.append(runtime)
        return runtime

    try:
        yield _open
    finally:
        for runtime in opened:
            await runtime.close()


@pytest.fixture
def submit(seeded: Any, tmp_path: Path) -> Callable[..., Any]:
    async def _submit(message: str = "check checkout") -> uuid.UUID:
        app = create_app(_settings(seeded.db, tmp_path))
        body = {
            "dev_identity": {
                "workspace_id": WS,
                "agent_id": "agent-u",
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
        return uuid.UUID(response.json()["run_id"])

    return _submit


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(50):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


def model_call(seeded: Any, run_id: uuid.UUID) -> dict[str, Any]:
    with psycopg.connect(seeded.db.migrator_dsn, row_factory=dict_row) as conn:
        found = conn.execute(
            "SELECT provider, model, status, input_tokens, output_tokens, cost_usd, error_code"
            " FROM tether.model_calls WHERE run_id = %s ORDER BY turn_ordinal",
            (run_id,),
        ).fetchall()
    assert len(found) == 1, f"expected exactly one model call, got {found}"
    return dict(found[0])


@pytest.mark.asyncio
async def test_a_successful_call_persists_the_usage_the_provider_reported(
    runtime_for: Runtime, submit: Callable[..., Any], seeded: Any
) -> None:
    turn = ModelTurn(text="all good", usage=ModelUsage(input_tokens=123, output_tokens=45))
    runtime = await runtime_for(_StubProvider(turn=turn))
    run_id = await submit()
    await drain(runtime)
    row = model_call(seeded, run_id)
    assert row["status"] == "succeeded"
    assert (row["input_tokens"], row["output_tokens"]) == (123, 45)
    assert row["error_code"] is None
    # Dollar cost is deferred (D-7): nothing derives a price.
    assert row["cost_usd"] is None


@pytest.mark.asyncio
async def test_a_provider_that_reports_no_usage_stores_nulls(
    runtime_for: Runtime, submit: Callable[..., Any], seeded: Any
) -> None:
    """Usage is never estimated: unknown stays unknown rather than becoming zero."""
    runtime = await runtime_for(_StubProvider(turn=ModelTurn(text="all good")))
    run_id = await submit()
    await drain(runtime)
    row = model_call(seeded, run_id)
    assert row["status"] == "succeeded"
    assert (row["input_tokens"], row["output_tokens"]) == (None, None)


@pytest.mark.asyncio
async def test_a_partially_reported_usage_keeps_the_known_half(
    runtime_for: Runtime, submit: Callable[..., Any], seeded: Any
) -> None:
    turn = ModelTurn(text="all good", usage=ModelUsage(input_tokens=7))
    runtime = await runtime_for(_StubProvider(turn=turn))
    run_id = await submit()
    await drain(runtime)
    row = model_call(seeded, run_id)
    assert (row["input_tokens"], row["output_tokens"]) == (7, None)


@pytest.mark.asyncio
async def test_a_failed_call_persists_the_normalised_error_code(
    runtime_for: Runtime, submit: Callable[..., Any], seeded: Any
) -> None:
    runtime = await runtime_for(_StubProvider(error=ProviderRateLimitedError(PROSE)))
    run_id = await submit()
    await drain(runtime)
    row = model_call(seeded, run_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "rate_limited"
    assert (row["input_tokens"], row["output_tokens"]) == (None, None)


@pytest.mark.asyncio
async def test_the_raw_provider_error_text_is_never_persisted(
    runtime_for: Runtime, submit: Callable[..., Any], seeded: Any
) -> None:
    """Only the normalised code is authoritative; provider prose can quote the request."""
    runtime = await runtime_for(_StubProvider(error=ProviderRateLimitedError(PROSE)))
    run_id = await submit()
    await drain(runtime)
    row = model_call(seeded, run_id)
    rendered = " ".join(str(value) for value in row.values())
    assert "hunter2" not in rendered
    assert "quota exceeded" not in rendered
    assert "org-abc" not in rendered
    assert row["error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_the_scripted_provider_still_records_a_successful_call(
    runtime_for: Runtime, submit: Callable[..., Any], seeded: Any, tmp_path: Path
) -> None:
    """The deterministic provider reports no usage, and that stays truthful rather than zero."""
    runtime = await runtime_for(ScriptedProvider(tmp_path, env="test"))
    run_id = await submit("check checkout #scenario=answer")
    await drain(runtime)
    row = model_call(seeded, run_id)
    assert (row["provider"], row["model"]) == ("scripted", "scripted-v1")
    assert row["status"] == "succeeded"
    assert (row["input_tokens"], row["output_tokens"], row["error_code"]) == (None, None, None)
