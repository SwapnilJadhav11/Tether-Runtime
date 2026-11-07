"""MVP M3: neutral history and the scripted provider (AD-06, PD-23)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tether.providers.history import NeutralMessage
from tether.testing.scripted import ScenarioError, ScriptedProvider

SCENARIO = """
name: fix_it
turns:
  - tool_calls:
      - tool: get_service_health
        arguments: {service: checkout}
  - tool_calls:
      - tool: restart_service
        arguments: {service: checkout}
  - final: done
"""


@pytest.fixture
def provider(tmp_path: Path) -> ScriptedProvider:
    (tmp_path / "fix_it.yaml").write_text(SCENARIO, encoding="utf-8")
    return ScriptedProvider(tmp_path, env="test")


def _history(*assistant_turns: int) -> list[NeutralMessage]:
    history = [NeutralMessage(role="user", content="please #scenario=fix_it")]
    for _ in assistant_turns:
        history.append(NeutralMessage(role="assistant", content=""))
    return history


@pytest.mark.asyncio
async def test_turns_follow_the_scenario_by_history_position(provider: ScriptedProvider) -> None:
    first = await provider.complete(_history(), manifest=[])
    assert [(c.tool, c.arguments) for c in first.tool_calls] == [
        ("get_service_health", {"service": "checkout"})
    ]
    second = await provider.complete(_history(1), manifest=[])
    assert second.tool_calls[0].tool == "restart_service"
    final = await provider.complete(_history(1, 2), manifest=[])
    assert final.tool_calls == ()
    assert final.text == "done"


@pytest.mark.asyncio
async def test_responses_are_deterministic_for_replay(provider: ScriptedProvider) -> None:
    assert await provider.complete(_history(), manifest=[]) == await provider.complete(
        _history(), manifest=[]
    )


@pytest.mark.asyncio
async def test_unknown_or_missing_scenario_is_an_error(provider: ScriptedProvider) -> None:
    with pytest.raises(ScenarioError):
        await provider.complete([NeutralMessage(role="user", content="no marker")], manifest=[])
    with pytest.raises(ScenarioError):
        await provider.complete(
            [NeutralMessage(role="user", content="#scenario=missing")], manifest=[]
        )


@pytest.mark.asyncio
async def test_running_past_the_script_is_an_error(provider: ScriptedProvider) -> None:
    with pytest.raises(ScenarioError):
        await provider.complete(_history(1, 2, 3), manifest=[])


def test_the_scripted_provider_is_refused_in_prod(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError):
        ScriptedProvider(tmp_path, env="prod")
