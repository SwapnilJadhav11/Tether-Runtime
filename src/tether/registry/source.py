"""``ToolSource``: where governed tools come from (§7, ADR-0011, ADR-0021 rules 12-14).

A source is workload-scoped. It lists the tools offered to new model turns (one version per name)
and resolves an exact pinned ``(name, version)``, offered or retained. It never falls forward to
another version, and its answers do not change for the life of the process. Agent filtering and
the manifest stay in ``ToolRegistry``. MCP or remote sources, later, implement the same protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tether.sdk import ToolDescriptor, Workload


@runtime_checkable
class ToolSource(Protocol):
    @property
    def name(self) -> str:
        """The source's identity, for errors and telemetry."""
        ...

    def list_tools(self) -> tuple[ToolDescriptor, ...]:
        """The offered tools: the version a new model turn may call, one per name."""
        ...

    def resolve(self, name: str, version: str) -> ToolDescriptor | None:
        """Exactly ``(name, version)``, offered or retained; ``None`` if it is not available."""
        ...


class NativePythonToolSource:
    """The V1 source: a workload's Python descriptors, snapshotted when it is constructed."""

    def __init__(self, workload: Workload) -> None:
        self._name = workload.name
        self._offered = tuple(workload.tools)
        self._pinned = {
            (tool.name, tool.version): tool for tool in (*workload.tools, *workload.retained_tools)
        }

    @property
    def name(self) -> str:
        return self._name

    def list_tools(self) -> tuple[ToolDescriptor, ...]:
        return self._offered

    def resolve(self, name: str, version: str) -> ToolDescriptor | None:
        return self._pinned.get((name, version))
