"""Tool, agent and workload declarations: the types a workload hands to Tether (§7, ADR-0002)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

Effect = Literal["read", "write"]
IdempotencySupport = Literal["supported", "unsupported"]

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


class Sensitive:
    """Marker for fields that must never be persisted, logged or shown unmasked (ADR-0017).

    Use as ``Annotated[str, Sensitive]`` on tool input or output model fields.
    """


class ToolNotApplied(Exception):  # noqa: N818 - an outcome, not an error: the call did nothing
    """Raised by a tool to report, with certainty, that it did not apply its effect."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ToolContext:
    """What Tether passes to a tool call. Forward ``idempotency_key`` on every write."""

    idempotency_key: str


ToolHandler = Callable[[Any, ToolContext], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    effect: Effect
    handler: ToolHandler
    # Required for writes: whether the target honours the forwarded idempotency key.
    idempotency: IdempotencySupport | None = None
    # Send attempts per call (PD-14), shared by retries and redeliveries. Only reads and
    # idempotent writes are ever re-sent; a non-idempotent write is sent at most once.
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"tool {self.name}: max_attempts must be at least 1")
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"invalid tool name {self.name!r}")
        if not self.version or not self.description:
            raise ValueError(f"tool {self.name}: version and description are required")
        if self.effect not in ("read", "write"):
            raise ValueError(f"tool {self.name}: effect must be read or write")
        if self.effect == "write" and self.idempotency is None:
            raise ValueError(f"write tool {self.name} must declare its idempotency support")


@dataclass(frozen=True)
class AgentDefinition:
    system_prompt: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Workload:
    name: str
    tools: tuple[ToolDescriptor, ...]
    agent: AgentDefinition

    def __post_init__(self) -> None:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"workload {self.name}: duplicate tool names")
        unknown = set(self.agent.tools) - set(names)
        if unknown:
            raise ValueError(
                f"workload {self.name}: agent offers unknown tool(s) {sorted(unknown)}"
            )
