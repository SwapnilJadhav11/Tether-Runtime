"""T-11: the provider contract every ``ModelProvider`` must satisfy (ADR-0016, AD-06).

These tests are provider-agnostic on purpose. They run unchanged against the scripted provider
and against the OpenAI adapter over a mocked transport, which is what "the same graph, tools and
policy run unchanged on either provider" has to mean at the boundary. A future adapter is proven
by making it a ``ProviderCase`` in ``conftest.py`` and changing nothing else.
"""

from __future__ import annotations

import pytest
from tests.contract.providers.harness import (
    MANIFEST,
    ProviderCase,
    TurnSpec,
    assert_neutral_turn,
    complete,
)

from tether.providers.history import ModelTurn, NeutralMessage, ToolCallProposal
from tether.providers.protocol import ProviderCapabilities

pytestmark = pytest.mark.asyncio


async def test_t11_a_plain_final_answer_is_neutral_text(case: ProviderCase) -> None:
    spec = TurnSpec(text="checkout is healthy; nothing to do")
    provider, history = case.build(spec)
    turn = await complete(provider, history)
    assert_neutral_turn(turn, spec)
    assert turn.tool_calls == ()


async def test_t11_b_one_tool_call_becomes_one_neutral_proposal(case: ProviderCase) -> None:
    spec = TurnSpec(calls=(("get_service_health", {"service": "checkout"}),))
    provider, history = case.build(spec)
    turn = await complete(provider, history)
    assert_neutral_turn(turn, spec)
    assert len(turn.tool_calls) == 1
    assert isinstance(turn.tool_calls[0], ToolCallProposal)


async def test_t11_c_several_tool_calls_keep_their_order(case: ProviderCase) -> None:
    """ADR-0012 governs a multi-call turn in sequence, so order is part of the contract."""
    spec = TurnSpec(
        calls=(
            ("get_service_health", {"service": "checkout"}),
            ("restart_service", {"service": "checkout"}),
        )
    )
    provider, history = case.build(spec)
    turn = await complete(provider, history)
    assert_neutral_turn(turn, spec)
    assert [call.tool for call in turn.tool_calls] == ["get_service_health", "restart_service"]


async def test_t11_d_structured_arguments_survive_translation(case: ProviderCase) -> None:
    """Arguments are structured data, not a string: nesting and types must round-trip."""
    arguments = {
        "service": "checkout",
        "options": {"force": True, "replicas": 3, "tags": ["a", "b"]},
        "note": "unicode: cafe ok",
    }
    spec = TurnSpec(calls=(("restart_service", arguments),))
    provider, history = case.build(spec)
    turn = await complete(provider, history)
    assert turn.tool_calls[0].arguments == arguments


async def test_t11_e_tool_results_in_history_are_accepted(case: ProviderCase) -> None:
    """A provider must accept a history that already contains a governed call and its result."""
    spec = TurnSpec(text="restart applied; closing out")
    provider, history = case.build(spec)
    full_history = [
        *history,
        NeutralMessage(
            role="assistant",
            tool_calls=(
                ToolCallProposal(
                    call_id="call_0", tool="restart_service", arguments={"service": "checkout"}
                ),
            ),
        ),
        NeutralMessage(
            role="tool", call_id="call_0", outcome="applied", content='{"restarted": true}'
        ),
    ]
    turn = await complete(provider, full_history)
    assert isinstance(turn, ModelTurn)


async def test_t11_f_usage_is_reported_when_the_provider_knows_it(case: ProviderCase) -> None:
    """Authoritative usage is passed through; a provider that has none reports none."""
    spec = TurnSpec(text="done", usage=(123, 45))
    provider, history = case.build(spec)
    turn = await complete(provider, history)
    if case.reports_usage:
        assert turn.usage is not None
        assert (turn.usage.input_tokens, turn.usage.output_tokens) == (123, 45)
    else:
        assert turn.usage is None


async def test_t11_g_capabilities_are_declared(case: ProviderCase) -> None:
    """ADR-0016 rule 2: the interface declares capabilities, honestly."""
    provider, _history = case.build(TurnSpec(text="done"))
    capabilities = provider.capabilities()
    assert isinstance(capabilities, ProviderCapabilities)
    assert capabilities.tool_calling is True
    assert isinstance(capabilities.parallel_tool_calls_controllable, bool)
    window = capabilities.context_window_tokens
    assert window is None or (isinstance(window, int) and window > 0)


async def test_t11_h_the_manifest_is_the_only_tool_source(case: ProviderCase) -> None:
    """A provider offered no tools must not invent one."""
    spec = TurnSpec(text="I have no tools")
    provider, history = case.build(spec)
    turn = await complete(provider, history, manifest=[])
    assert turn.tool_calls == ()


async def test_t11_i_the_boundary_returns_only_tether_types(case: ProviderCase) -> None:
    """No provider SDK or transport object may cross the boundary (TDR-003)."""
    spec = TurnSpec(calls=(("get_service_health", {"service": "checkout"}),), usage=(10, 2))
    provider, history = case.build(spec)
    turn = await complete(provider, history, MANIFEST)
    assert type(turn) is ModelTurn
    for call in turn.tool_calls:
        assert type(call) is ToolCallProposal
        assert type(call.call_id) is str
        assert type(call.tool) is str
        assert type(call.arguments) is dict
    modules = {type(turn).__module__} | {type(c).__module__ for c in turn.tool_calls}
    if turn.usage is not None:
        modules.add(type(turn.usage).__module__)
    assert all(module.startswith("tether.") for module in modules), modules
