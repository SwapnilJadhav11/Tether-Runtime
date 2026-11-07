"""Provider-neutral conversation history and model turns (AD-06, ADR-0016).

Tool payloads in history are masked; tool results carry a neutral ``CallOutcome``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tether.core.results import CallOutcome


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolCallProposal(_Frozen):
    """One tool call proposed by the model. Untrusted: validated and governed before use."""

    call_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class NeutralMessage(_Frozen):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCallProposal, ...] = ()
    # For role == "tool": the call this result answers, and its neutral outcome.
    call_id: str | None = None
    outcome: CallOutcome | None = None


class ModelTurn(_Frozen):
    """A model response: tool calls, or a final answer when there are none."""

    text: str = ""
    tool_calls: tuple[ToolCallProposal, ...] = ()
