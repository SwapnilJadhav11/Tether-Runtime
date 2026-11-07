"""Workload loading by entry point and the per-workload tool registry (§7, ADR-0002).

Core never imports workload code statically: a workload is found by name in the
``tether.workloads`` entry-point group and must resolve to a ``tether.sdk.Workload``.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from tether.sdk import ToolDescriptor, Workload

ENTRY_POINT_GROUP = "tether.workloads"


class WorkloadLoadError(Exception):
    """A workload named in configuration is missing or broken. Startup fails closed."""


def load_workload(name: str) -> Workload:
    matches = entry_points(group=ENTRY_POINT_GROUP, name=name)
    if not matches:
        raise WorkloadLoadError(f"no workload named {name!r} is installed")
    try:
        workload = next(iter(matches)).load()
    except Exception as error:
        raise WorkloadLoadError(f"workload {name!r} failed to load: {error}") from error
    if not isinstance(workload, Workload) or workload.name != name:
        raise WorkloadLoadError(f"entry point {name!r} does not provide a matching Workload")
    return workload


class ToolRegistry:
    def __init__(self, workload: Workload) -> None:
        self.workload = workload
        self._tools = {tool.name: tool for tool in workload.tools}

    def resolve(self, name: str) -> ToolDescriptor | None:
        return self._tools.get(name)

    def offered(self, name: str) -> ToolDescriptor | None:
        """A tool the agent is allowed to call; anything else is an invalid proposal."""
        return self._tools.get(name) if name in self.workload.agent.tools else None

    def manifest(self) -> list[dict[str, Any]]:
        """Provider-neutral manifest of the agent's tools: name, description, input schema."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_model.model_json_schema(),
            }
            for tool in (self._tools[name] for name in self.workload.agent.tools)
        ]
