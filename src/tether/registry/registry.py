"""Workload loading by entry point and the per-workload tool registry (§7, ADR-0002).

Core never imports workload code statically: a workload is found by name in the
``tether.workloads`` entry-point group and must resolve to a ``tether.sdk.Workload``.

``ToolRegistry`` sits on a ``ToolSource`` (ADR-0021 rule 12): it offers the agent's tools to new
model turns and renders their manifest, and it resolves pinned versions through the source.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from tether.registry.source import NativePythonToolSource, ToolSource
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
    def __init__(self, workload: Workload, source: ToolSource | None = None) -> None:
        self.workload = workload
        self.source: ToolSource = source or NativePythonToolSource(workload)
        self._offered = {tool.name: tool for tool in self.source.list_tools()}

    def offered(self, name: str) -> ToolDescriptor | None:
        """The current version of a tool the agent may call; anything else is invalid."""
        return self._offered.get(name) if name in self.workload.agent.tools else None

    def resolve(self, name: str, version: str) -> ToolDescriptor | None:
        """Exactly the pinned ``(name, version)``; never another version (ADR-0011 rule 1)."""
        return self.source.resolve(name, version)

    def manifest(self) -> list[dict[str, Any]]:
        """Provider-neutral manifest of the agent's tools: name, description, input schema."""
        offered = (self._offered.get(name) for name in self.workload.agent.tools)
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_model.model_json_schema(),
            }
            for tool in offered
            if tool is not None
        ]
