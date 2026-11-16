"""T-22: the neutral contract can carry a malformed tool call (W3.4, ADR-0016 rule 4).

An adapter that cannot parse a call's arguments hands it over as ``malformed`` with no
arguments, so the run graph can give it a typed ``invalid`` result instead of failing the run.
A malformed proposal never carries arguments: nothing that might accidentally validate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tether.providers.history import NeutralMessage, ToolCallProposal
from tether.testing.scripted import ScriptedProvider


def test_t22_a_proposal_is_well_formed_by_default() -> None:
    assert ToolCallProposal(call_id="c", tool="t", arguments={"a": 1}).malformed is False


def test_t22_a_malformed_proposal_carries_no_arguments() -> None:
    call = ToolCallProposal(call_id="c", tool="t", malformed=True)
    assert (call.arguments, call.malformed) == ({}, True)
    with pytest.raises(ValidationError, match="malformed"):
        ToolCallProposal(call_id="c", tool="t", arguments={"a": 1}, malformed=True)


def test_t22_a_malformed_proposal_round_trips_through_history() -> None:
    call = ToolCallProposal(call_id="c", tool="t", malformed=True)
    message = NeutralMessage(role="assistant", tool_calls=(call,))
    assert NeutralMessage.model_validate(message.model_dump(mode="json")) == message


def test_t22_history_from_before_w34_reads_as_well_formed() -> None:
    old = {"role": "assistant", "tool_calls": [{"call_id": "c", "tool": "t", "arguments": {}}]}
    assert NeutralMessage.model_validate(old).tool_calls[0].malformed is False


@pytest.mark.asyncio
async def test_t22_the_scripted_provider_can_script_a_malformed_call(tmp_path: Path) -> None:
    (tmp_path / "m.yaml").write_text(
        "name: m\nturns:\n  - tool_calls: [{tool: get_service_health, malformed: true}]\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(tmp_path, env="test")
    turn = await provider.complete([NeutralMessage(role="user", content="#scenario=m")], [])
    [call] = turn.tool_calls
    assert (call.tool, call.arguments, call.malformed) == ("get_service_health", {}, True)
