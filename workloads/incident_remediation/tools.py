"""Reference workload tools: one read, one idempotent write."""

from __future__ import annotations

from pydantic import BaseModel, Field

from tether.sdk import ToolContext, ToolDescriptor, ToolNotApplied
from workloads.incident_remediation.client import http_client


class ServiceRef(BaseModel):
    service: str = Field(min_length=1, max_length=64, description="Service name")


class ServiceHealth(BaseModel):
    service: str
    healthy: bool
    restarts: int


async def _get_service_health(args: ServiceRef, ctx: ToolContext) -> ServiceHealth:
    async with http_client() as http:
        response = await http.get(f"/services/{args.service}")
    if response.status_code == 404:
        raise ToolNotApplied("unknown service")
    response.raise_for_status()
    return ServiceHealth.model_validate(response.json())


async def _restart_service(args: ServiceRef, ctx: ToolContext) -> ServiceHealth:
    async with http_client() as http:
        response = await http.post(
            f"/services/{args.service}/restart", headers={"Idempotency-Key": ctx.idempotency_key}
        )
    if response.status_code in (404, 422):
        raise ToolNotApplied(str(response.json().get("detail", "rejected")))
    response.raise_for_status()
    return ServiceHealth.model_validate(response.json())


get_service_health = ToolDescriptor(
    name="get_service_health",
    version="1.0.0",
    description="Read a service's health and restart count.",
    input_model=ServiceRef,
    output_model=ServiceHealth,
    effect="read",
    handler=_get_service_health,
)

restart_service = ToolDescriptor(
    name="restart_service",
    version="1.0.0",
    description="Restart a service. Requires approval.",
    input_model=ServiceRef,
    output_model=ServiceHealth,
    effect="write",
    idempotency="supported",
    handler=_restart_service,
)
