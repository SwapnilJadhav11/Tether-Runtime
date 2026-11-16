"""T-10 (retry variant): bounded, stop-gated provider retry (W3.3, ADR-0016 rules 3 and 5).

A retryable provider error is retried at most ``provider_max_attempts`` times per model turn.
The stop predicate is checked before every attempt and right after every response; every
attempt, failed or not, gets its own ``model_calls`` row with its real attempt number. A
non-retryable error is never retried. The delay between attempts is the PD-13 backoff.
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
from pydantic import SecretStr

import tether.runtime.graph as graph_module
from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, ModelUsage, NeutralMessage
from tether.providers.protocol import (
    ProviderAuthError,
    ProviderCapabilities,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.worker.main import process_next_job

WS = "ws-retry"
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": "agent-r", "workload": "incident_remediation"}],
                "policy": {
                    "rules": [{"tool": "get_service_health", "decision": "allow"}],
                    "role_permissions": {"operator": ["service:read"]},
                    "agent_permissions": {"agent-r": ["service:read"]},
                },
            }
        ]
    }
)


class _SequenceProvider:
    """Raises the queued errors in order, then answers; counts every attempt it receives."""

    name = "stub"
    model = "stub-v1"

    def __init__(self, errors: Sequence[Exception], *, always: Exception | None = None) -> None:
        self._errors = list(errors)
        self._always = always
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(tool_calling=True, parallel_tool_calls_controllable=True)

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        self.calls += 1
        if self._always is not None:
            raise self._always
        if self._errors:
            raise self._errors.pop(0)
        return ModelTurn(text="all good", usage=ModelUsage(input_tokens=11, output_tokens=3))


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


Runtime = Callable[..., Any]


@pytest.fixture(autouse=True)
def no_real_pause(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff delays instead of sleeping them."""
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(graph_module, "_sleep", record)
    return delays


@pytest_asyncio.fixture
async def runtime_for(seeded: Any, tmp_path: Path) -> AsyncIterator[Runtime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    opened: list[WorkerRuntime] = []

    async def _open(provider: Any, **overrides: Any) -> WorkerRuntime:
        runtime = await WorkerRuntime.open(
            _settings(seeded.db, tmp_path, **overrides), bootstrap=BOOT, provider=provider
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


async def submit(seeded: Any, tmp_path: Path) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": "agent-r",
            "requester": {"sub": "alice", "roles": ["operator"]},
        },
        "task_input": {"message": "check checkout"},
    }
    response = await call_api(seeded, tmp_path, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def cancel(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> None:
    body = {"dev_identity": {"workspace_id": WS, "sub": "alice", "roles": []}}
    response = await call_api(
        seeded, tmp_path, "POST", f"/v1/tasks/runs/{run_id}/cancel", json=body
    )
    assert response.status_code == 202, response.text


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def pass_deadline(seeded: Any, run_id: uuid.UUID) -> None:
    rows(
        seeded,
        "UPDATE tether.runs SET deadline_at = clock_timestamp() WHERE id = %s RETURNING id",
        run_id,
    )


def attempts(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    """(turn_ordinal, attempt, status, error_code, occurrence_id) per model_calls row."""
    return rows(
        seeded,
        "SELECT turn_ordinal, attempt, status, error_code, occurrence_id FROM tether.model_calls"
        " WHERE run_id = %s ORDER BY started_at, attempt",
        run_id,
    )


def run_row(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return rows(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(50):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


@pytest.mark.asyncio
async def test_t10_two_retryable_failures_then_success_record_three_attempts(
    runtime_for: Runtime, seeded: Any, tmp_path: Path, no_real_pause: list[float]
) -> None:
    provider = _SequenceProvider([ProviderTimeoutError(), ProviderUnavailableError()])
    runtime = await runtime_for(provider)
    run_id = await submit(seeded, tmp_path)
    await drain(runtime)
    found = attempts(seeded, run_id)
    assert [(t, a, s, e) for t, a, s, e, _ in found] == [
        (0, 1, "failed", "timeout"),
        (0, 2, "failed", "unavailable"),
        (0, 3, "succeeded", None),
    ]
    assert len({occurrence for *_, occurrence in found}) == 3
    assert provider.calls == 3
    assert run_row(seeded, run_id) == ("succeeded", "all good")
    # PD-13: full jitter under a ceiling of 0.5 s, then 1 s.
    assert len(no_real_pause) == 2
    assert 0.0 <= no_real_pause[0] <= 0.5
    assert 0.0 <= no_real_pause[1] <= 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [3, 2])
async def test_t10_a_provider_that_always_fails_retryably_ends_the_run_after_the_budget(
    runtime_for: Runtime, seeded: Any, tmp_path: Path, budget: int
) -> None:
    provider = _SequenceProvider([], always=ProviderRateLimitedError())
    overrides = {} if budget == 3 else {"provider_max_attempts": budget}
    runtime = await runtime_for(provider, **overrides)
    run_id = await submit(seeded, tmp_path)
    await drain(runtime)
    found = attempts(seeded, run_id)
    assert [(a, s, e) for _, a, s, e, _ in found] == [
        (n, "failed", "rate_limited") for n in range(1, budget + 1)
    ]
    assert provider.calls == budget
    status, summary = run_row(seeded, run_id)
    assert status == "failed"
    assert summary.startswith("model call failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ProviderAuthError(), ProviderError()])
async def test_t10_a_non_retryable_error_is_not_retried(
    runtime_for: Runtime, seeded: Any, tmp_path: Path, error: Exception, no_real_pause: list[float]
) -> None:
    provider = _SequenceProvider([error])
    runtime = await runtime_for(provider)
    run_id = await submit(seeded, tmp_path)
    await drain(runtime)
    found = attempts(seeded, run_id)
    assert [(a, s) for _, a, s, _, _ in found] == [(1, "failed")]
    assert provider.calls == 1
    assert no_real_pause == []
    assert run_row(seeded, run_id)[0] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "status", "summary"),
    [("cancel", "cancelled", "cancelled by alice"), ("deadline", "timed_out", "deadline passed")],
)
async def test_t10_a_stop_committed_between_attempts_makes_no_further_provider_call(
    runtime_for: Runtime,
    seeded: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    status: str,
    summary: str,
) -> None:
    provider = _SequenceProvider([], always=ProviderUnavailableError())
    runtime = await runtime_for(provider)
    run_id = await submit(seeded, tmp_path)

    async def stop_during_backoff(seconds: float) -> None:
        # The failed attempt's row and its post-response stop check are already committed.
        if kind == "cancel":
            await cancel(seeded, tmp_path, run_id)
        else:
            pass_deadline(seeded, run_id)

    monkeypatch.setattr(graph_module, "_sleep", stop_during_backoff)
    await drain(runtime)
    assert provider.calls == 1
    assert [(a, s) for _, a, s, _, _ in attempts(seeded, run_id)] == [(1, "failed")]
    assert run_row(seeded, run_id) == (status, summary)


@pytest.mark.asyncio
async def test_t10_a_stop_committed_while_an_attempt_is_in_flight_is_not_retried(
    runtime_for: Runtime, seeded: Any, tmp_path: Path, no_real_pause: list[float]
) -> None:
    """The post-response check runs after a failed attempt too: no backoff, no retry."""
    run_ids: list[uuid.UUID] = []

    class _CancelThenFail(_SequenceProvider):
        async def complete(
            self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
        ) -> ModelTurn:
            self.calls += 1
            await cancel(seeded, tmp_path, run_ids[0])
            raise ProviderUnavailableError()

    provider = _CancelThenFail([])
    runtime = await runtime_for(provider)
    run_ids.append(await submit(seeded, tmp_path))
    await drain(runtime)
    assert provider.calls == 1
    assert no_real_pause == []
    assert [(a, s, e) for _, a, s, e, _ in attempts(seeded, run_ids[0])] == [
        (1, "failed", "unavailable")
    ]
    assert run_row(seeded, run_ids[0]) == ("cancelled", "cancelled by alice")


def test_t10_the_provider_attempt_budget_is_a_positive_setting_defaulting_to_three(
    tmp_path: Path,
) -> None:
    settings = Settings(database_url=SecretStr("postgresql://x"), env="test")
    assert settings.provider_max_attempts == 3
    with pytest.raises(ValueError, match="provider_max_attempts"):
        Settings(database_url=SecretStr("postgresql://x"), env="test", provider_max_attempts=0)
