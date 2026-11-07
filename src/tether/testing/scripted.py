"""Scripted model provider driven by YAML scenarios (PD-23). Refused in prod.

A response is a pure function of (scenario, turn index), where the turn index is the number of
assistant messages already in history, so a crash replay gets the identical response. The
scenario is selected by a ``#scenario=<name>`` marker in the first user message.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tether.providers.history import ModelTurn, NeutralMessage, ToolCallProposal
from tether.providers.protocol import ProviderError

_MARKER = re.compile(r"#scenario=([A-Za-z0-9_-]+)")


class ScenarioError(ProviderError):
    """No usable scenario: missing marker, unknown name, bad file, or the script ran out."""


class _ScriptedCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class _Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_calls: list[_ScriptedCall] = Field(default_factory=list)
    final: str | None = None


class _Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    turns: list[_Turn]


class ScriptedProvider:
    name = "scripted"
    model = "scripted-v1"

    def __init__(self, scenarios_dir: Path, *, env: str) -> None:
        if env == "prod":
            raise ScenarioError("the scripted provider is for dev and test only")
        self._dir = scenarios_dir

    def _scenario(self, history: Sequence[NeutralMessage]) -> _Scenario:
        first_user = next((m for m in history if m.role == "user"), None)
        match = _MARKER.search(first_user.content) if first_user else None
        if match is None:
            raise ScenarioError("no #scenario=<name> marker in the task")
        path = self._dir / f"{match.group(1)}.yaml"
        try:
            return _Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise ScenarioError(f"scenario {match.group(1)!r} is unavailable: {error}") from None

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        scenario = self._scenario(history)
        index = sum(1 for message in history if message.role == "assistant")
        if index >= len(scenario.turns):
            raise ScenarioError(f"scenario {scenario.name!r} has no turn {index}")
        turn = scenario.turns[index]
        if turn.final is not None:
            return ModelTurn(text=turn.final)
        return ModelTurn(
            tool_calls=tuple(
                ToolCallProposal(call_id=f"call_{index}_{i}", tool=c.tool, arguments=c.arguments)
                for i, c in enumerate(turn.tool_calls)
            )
        )
