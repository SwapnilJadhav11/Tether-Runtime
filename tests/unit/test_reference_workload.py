"""MVP M2: reference workload tools against the demo target (in process)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from services.demo_target_service.app import create_app
from workloads.incident_remediation import client, tools

from tether.sdk import ToolContext


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[None]:
    client.set_transport(httpx.ASGITransport(app=create_app()))
    yield
    client.set_transport(None)


@pytest.mark.asyncio
async def test_health_reports_the_unhealthy_service() -> None:
    result = await tools.get_service_health.handler(
        tools.ServiceRef(service="checkout"), ToolContext(idempotency_key="unused")
    )
    assert result.model_dump() == {"service": "checkout", "healthy": False, "restarts": 0}


@pytest.mark.asyncio
async def test_restart_with_the_same_idempotency_key_applies_once() -> None:
    ctx = ToolContext(idempotency_key="tth_same")
    first = await tools.restart_service.handler(tools.ServiceRef(service="checkout"), ctx)
    second = await tools.restart_service.handler(tools.ServiceRef(service="checkout"), ctx)
    assert first == second
    assert first.restarts == 1
    other = await tools.restart_service.handler(
        tools.ServiceRef(service="checkout"), ToolContext(idempotency_key="tth_other")
    )
    assert other.restarts == 2
    health = await tools.get_service_health.handler(
        tools.ServiceRef(service="checkout"), ToolContext(idempotency_key="unused")
    )
    assert health.healthy is True
