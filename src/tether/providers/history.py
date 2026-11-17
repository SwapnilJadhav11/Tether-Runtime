"""Provider-neutral conversation history and model turns (AD-06, ADR-0016).

Tool payloads in history are masked; tool results carry a neutral ``CallOutcome``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tether.core.results import CallOutcome


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolCallProposal(_Frozen):
    """One tool call proposed by the model. Untrusted: validated and governed before use.

    ``malformed``: the adapter could not parse the call's arguments into a JSON object. Such a
    call carries no arguments (nothing that might accidentally validate) and never the raw text
    (ADR-0017); the run graph gives it a typed ``invalid`` result inside the repair budget
    (ADR-0016 rule 4).
    """

    call_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    malformed: bool = False

    @model_validator(mode="after")
    def _malformed_has_no_arguments(self) -> ToolCallProposal:
        if self.malformed and self.arguments:
            raise ValueError("a malformed proposal carries no arguments")
        return self


class NeutralMessage(_Frozen):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCallProposal, ...] = ()
    # For role == "tool": the call this result answers, and its neutral outcome.
    call_id: str | None = None
    outcome: CallOutcome | None = None


class ModelUsage(_Frozen):
    """Token usage as the provider reported it (ADR-0016 rule 5).

    Never estimated: a field is ``None`` when the provider did not report it. Dollar cost is
    deferred (D-7), so no price is derived here.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelTurn(_Frozen):
    """A model response: tool calls, or a final answer when there are none."""

    text: str = ""
    tool_calls: tuple[ToolCallProposal, ...] = ()
    # Transient: belongs to the `model_calls` row for this attempt, never to history.
    usage: ModelUsage | None = None
