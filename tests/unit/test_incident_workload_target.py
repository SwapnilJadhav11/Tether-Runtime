"""W9.3: the incident workload's extra tools against the demo target (in process).

The target enforces the config-version precondition atomically (T-21), replays a repeated
idempotency key, and has deterministic fault switches set only in process (no network control).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from services.demo_target_service.app import ADVERSARIAL_LOG_LINE, create_app
from workloads.incident_remediation import WORKLOAD, client, tools

from tether.sdk import ToolContext, ToolNotApplied


@pytest.fixture(autouse=True)
def target() -> Iterator[FastAPI]:
    app = create_app()
    client.set_transport(httpx.ASGITransport(app=app))
    yield app
    client.set_transport(None)


def change(version: int, *, key: str = "pool.size", value: str = "20") -> tools.ConfigChangeIn:
    return tools.ConfigChangeIn(
        service="checkout", key=key, value=value, expected_config_version=version
    )


async def current_config() -> dict[str, Any]:
    async with client.http_client() as http:
        return dict((await http.get("/services/checkout/config")).json())


def test_the_workload_registers_its_four_tools_for_its_agent() -> None:
    assert [tool.name for tool in WORKLOAD.tools] == list(WORKLOAD.agent.tools)
    assert {tool.name for tool in WORKLOAD.tools} == {
        "get_service_health",
        "get_service_logs",
        "restart_service",
        "update_service_config",
    }


def test_t21_the_config_tool_is_a_guarded_write_with_a_declared_precondition() -> None:
    descriptor = tools.update_service_config
    assert (descriptor.effect, descriptor.risk, descriptor.idempotency) == (
        "write",
        "high",
        "supported",
    )
    assert descriptor.preconditions == ("expected_config_version",)
    assert "expected_config_version" in tools.ConfigChangeIn.model_json_schema()["required"]


@pytest.mark.asyncio
async def test_t21_a_matching_version_applies_and_advances_the_version() -> None:
    result = await tools.update_service_config.handler(
        change(1), ToolContext(idempotency_key="tth_a")
    )
    assert (result.config_version, result.key, result.value) == (2, "pool.size", "20")
    config = await current_config()
    assert (config["config_version"], config["values"]["pool.size"]) == (2, "20")


@pytest.mark.asyncio
async def test_t21_a_stale_version_is_not_applied_and_changes_nothing() -> None:
    before = await current_config()
    with pytest.raises(ToolNotApplied, match="precondition_failed"):
        await tools.update_service_config.handler(change(0), ToolContext(idempotency_key="tth_b"))
    assert await current_config() == before


@pytest.mark.asyncio
async def test_a_repeated_key_returns_the_original_answer_and_applies_once() -> None:
    ctx = ToolContext(idempotency_key="tth_same")
    first = await tools.update_service_config.handler(change(1), ctx)
    second = await tools.update_service_config.handler(change(1), ctx)
    assert first == second
    assert (await current_config())["config_version"] == 2


@pytest.mark.asyncio
async def test_a_key_reused_for_a_different_change_is_not_applied() -> None:
    ctx = ToolContext(idempotency_key="tth_reused")
    await tools.update_service_config.handler(change(1), ctx)
    with pytest.raises(ToolNotApplied):
        await tools.update_service_config.handler(change(2, value="30"), ctx)
    assert (await current_config())["config_version"] == 2


@pytest.mark.asyncio
async def test_an_unknown_service_is_not_applied() -> None:
    args = tools.ConfigChangeIn(service="nope", key="k", value="v", expected_config_version=1)
    with pytest.raises(ToolNotApplied):
        await tools.update_service_config.handler(args, ToolContext(idempotency_key="tth_c"))


@pytest.mark.asyncio
async def test_logs_are_benign_until_the_adversarial_fixture_is_switched_on(
    target: FastAPI,
) -> None:
    args = tools.ServiceRef(service="checkout")
    benign = await tools.get_service_logs.handler(args, ToolContext(idempotency_key="u"))
    assert ADVERSARIAL_LOG_LINE not in benign.lines
    target.state.faults.adversarial_logs = True
    hostile = await tools.get_service_logs.handler(args, ToolContext(idempotency_key="u"))
    assert hostile.lines[-1] == ADVERSARIAL_LOG_LINE
    assert "auth.enabled=false" in ADVERSARIAL_LOG_LINE


@pytest.mark.asyncio
async def test_t09_a_malformed_health_answer_breaks_the_output_schema(target: FastAPI) -> None:
    target.state.faults.malformed_health = True
    with pytest.raises(ValueError, match="validation"):  # pydantic.ValidationError
        await tools.get_service_health.handler(
            tools.ServiceRef(service="checkout"), ToolContext(idempotency_key="u")
        )


@pytest.mark.parametrize(
    ("fault", "applied"), [("unavailable", 0), ("applied_then_unavailable", 1)]
)
@pytest.mark.asyncio
async def test_restart_faults_are_deterministic_and_the_key_still_dedupes(
    target: FastAPI, fault: str, applied: int
) -> None:
    target.state.faults.restart[:] = [fault]
    ctx = ToolContext(idempotency_key="tth_f")
    args = tools.ServiceRef(service="checkout")
    with pytest.raises(httpx.HTTPStatusError):
        await tools.restart_service.handler(args, ctx)
    health = await tools.get_service_health.handler(args, ctx)
    assert health.restarts == applied
    again = await tools.restart_service.handler(args, ctx)  # the script is spent: ok
    assert again.restarts == 1  # applied once in total, whichever way the first answer went


@pytest.mark.parametrize(
    ("fault", "version"), [("unavailable", 1), ("applied_then_unavailable", 2)]
)
@pytest.mark.asyncio
async def test_config_faults_are_deterministic_and_the_key_still_dedupes(
    target: FastAPI, fault: str, version: int
) -> None:
    target.state.faults.config[:] = [fault]
    ctx = ToolContext(idempotency_key="tth_g")
    with pytest.raises(httpx.HTTPStatusError):
        await tools.update_service_config.handler(change(1), ctx)
    assert (await current_config())["config_version"] == version
    await tools.update_service_config.handler(change(1), ctx)
    assert (await current_config())["config_version"] == 2
