"""MVP M2: SDK descriptors, workload loading by entry point, manifest (ADR-0002, §7)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tether.registry.registry import ToolRegistry, WorkloadLoadError, load_workload
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload

REFERENCE = "incident_remediation"


class In(BaseModel):
    name: str


class Out(BaseModel):
    ok: bool


async def _handler(args: In, ctx: ToolContext) -> Out:
    return Out(ok=True)


def _tool(name: str = "probe_tool", **overrides: object) -> ToolDescriptor:
    values: dict[str, object] = {
        "name": name,
        "version": "1.0.0",
        "description": "probe",
        "input_model": In,
        "output_model": Out,
        "effect": "read",
        "handler": _handler,
        **overrides,
    }
    return ToolDescriptor(**values)  # type: ignore[arg-type]


def test_reference_workload_loads_by_entry_point_name() -> None:
    workload = load_workload(REFERENCE)
    assert workload.name == REFERENCE
    effects = {tool.name: tool.effect for tool in workload.tools}
    assert "read" in effects.values()
    assert "write" in effects.values()


def test_unknown_workload_fails_closed() -> None:
    with pytest.raises(WorkloadLoadError):
        load_workload("no_such_workload")


def test_manifest_offers_the_agent_tools_with_input_schemas() -> None:
    workload = load_workload(REFERENCE)
    manifest = ToolRegistry(workload).manifest()
    assert {entry["name"] for entry in manifest} == set(workload.agent.tools)
    for entry in manifest:
        assert entry["description"]
        assert entry["input_schema"]["type"] == "object"


def test_resolve_returns_the_descriptor_or_none() -> None:
    registry = ToolRegistry(Workload(name="w", tools=(_tool(),), agent=AgentDefinition("p", ())))
    assert registry.resolve("probe_tool") is not None
    assert registry.resolve("missing") is None


def test_write_tools_must_declare_idempotency_support() -> None:
    with pytest.raises(ValueError, match="idempotency"):
        _tool(effect="write")
    assert _tool(effect="write", idempotency="supported").effect == "write"


def test_an_agent_cannot_offer_an_unknown_tool() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        Workload(name="w", tools=(_tool(),), agent=AgentDefinition("p", ("missing",)))


def test_tool_names_are_unique_within_a_workload() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Workload(name="w", tools=(_tool(), _tool()), agent=AgentDefinition("p", ()))
