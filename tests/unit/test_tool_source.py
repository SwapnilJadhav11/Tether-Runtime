"""W3.2: the ``ToolSource`` protocol and pinned-version resolution (ADR-0021 rules 12-14).

The source is workload-scoped: it lists the offered tools and resolves an exact
``(name, version)``, offered or retained, and nothing else. It never falls forward to another
version. ``ToolRegistry`` keeps the agent filter and the manifest on top of any source.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tether.registry.registry import ToolRegistry
from tether.registry.source import NativePythonToolSource, ToolSource
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload


class In(BaseModel):
    name: str


class Out(BaseModel):
    ok: bool


async def _handler(args: Any, ctx: ToolContext) -> Out:
    return Out(ok=True)


def tool(name: str, version: str) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version=version,
        description=f"{name} {version}",
        input_model=In,
        output_model=Out,
        effect="read",
        handler=_handler,
        risk="low",
        required_permissions=("probe:read",),
    )


CURRENT = tool("probe", "2.0.0")
RETAINED = tool("probe", "1.0.0")
HIDDEN = tool("hidden", "1.0.0")
WORKLOAD = Workload(
    name="w",
    tools=(CURRENT, HIDDEN),
    agent=AgentDefinition("p", ("probe",)),
    retained_tools=(RETAINED,),
)


def test_w3_2_the_native_source_satisfies_the_protocol() -> None:
    source = NativePythonToolSource(WORKLOAD)
    assert isinstance(source, ToolSource)
    assert source.name == "w"


def test_w3_2_list_tools_returns_the_offered_versions_only() -> None:
    listed = NativePythonToolSource(WORKLOAD).list_tools()
    assert [(t.name, t.version) for t in listed] == [("probe", "2.0.0"), ("hidden", "1.0.0")]


def test_w3_2_resolve_returns_exactly_the_pinned_version_offered_or_retained() -> None:
    source = NativePythonToolSource(WORKLOAD)
    assert source.resolve("probe", "2.0.0") is CURRENT
    assert source.resolve("probe", "1.0.0") is RETAINED


def test_w3_2_resolve_never_falls_forward_to_another_version() -> None:
    source = NativePythonToolSource(WORKLOAD)
    assert source.resolve("probe", "0.9.0") is None
    assert source.resolve("probe", "2.0.1") is None
    assert source.resolve("missing", "2.0.0") is None


def test_w3_2_the_registry_offers_only_the_agents_current_versions() -> None:
    registry = ToolRegistry(WORKLOAD)
    assert registry.offered("probe") is CURRENT
    assert registry.offered("hidden") is None  # not offered to this agent
    assert [entry["name"] for entry in registry.manifest()] == ["probe"]


def test_w3_2_the_registry_resolves_pinned_versions_through_its_source() -> None:
    registry = ToolRegistry(WORKLOAD)
    assert registry.resolve("probe", "1.0.0") is RETAINED
    assert registry.resolve("probe", "2.0.0") is CURRENT
    assert registry.resolve("probe", "3.0.0") is None


class StubSource:
    """Any implementation of the protocol, not only the native one, can back a registry."""

    name = "stub"

    def list_tools(self) -> tuple[ToolDescriptor, ...]:
        return (CURRENT,)

    def resolve(self, name: str, version: str) -> ToolDescriptor | None:
        return RETAINED if (name, version) == ("probe", "1.0.0") else None


def test_w3_2_a_registry_can_be_backed_by_another_tool_source() -> None:
    stub = StubSource()
    assert isinstance(stub, ToolSource)
    registry = ToolRegistry(WORKLOAD, source=stub)
    assert registry.offered("probe") is CURRENT
    assert registry.resolve("probe", "1.0.0") is RETAINED
    assert registry.resolve("probe", "2.0.0") is None
