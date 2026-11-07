"""Reference workload: incident remediation. A client of Tether through ``tether.sdk`` only."""

from __future__ import annotations

from tether.sdk import AgentDefinition, Workload
from workloads.incident_remediation.tools import get_service_health, restart_service

WORKLOAD = Workload(
    name="incident_remediation",
    tools=(get_service_health, restart_service),
    agent=AgentDefinition(
        system_prompt=(
            "You remediate service incidents. Check a service's health before acting, "
            "restart it only if it is unhealthy, then report what you did."
        ),
        tools=("get_service_health", "restart_service"),
    ),
)
