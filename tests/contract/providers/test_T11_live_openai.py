"""T-11 live: the OpenAI adapter against the real API (marker ``live``, manual dispatch only).

Opt in with two environment variables and nothing else::

    TETHER_MODEL_PROVIDER=openai
    TETHER_OPENAI_API_KEY=<the operator's key>   # read through SecretsProvider, never logged
    TETHER_OPENAI_MODEL=<an inexpensive model>   # no default: nobody spends on an unchosen model

    uv run poe test-live

Without the key every test here skips, so ordinary CI never calls OpenAI and never needs a
credential. What it proves: a real hosted model, selected by configuration only, produces a tool
proposal that arrives as the *same* provider-neutral ``ModelTurn`` the scripted provider
produces, with authoritative usage attached.

It sends only the synthetic prompt and tool manifest below: no repository contents, no customer
data, no credentials, and three short turns at most.
"""

from __future__ import annotations

import io
import os

import pytest
from pydantic import SecretStr
from tests.contract.providers.harness import (
    LIVE_KEY_ENV,
    MANIFEST,
    TurnSpec,
    assert_neutral_turn,
    assert_usage_is_honest,
    complete,
)

from tether.providers.factory import build_provider
from tether.providers.history import NeutralMessage, ToolCallProposal
from tether.providers.openai_provider import OpenAIProvider
from tether.secrets.env_provider import EnvSecretsProvider
from tether.settings import Settings

MODEL_ENV = "TETHER_OPENAI_MODEL"

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get(LIVE_KEY_ENV),
        reason=f"set {LIVE_KEY_ENV} and {MODEL_ENV} to run the live provider proof",
    ),
]

# Deliberately mundane and synthetic: nothing here is proprietary.
SYSTEM = (
    "You are an operations agent. Use the provided tools to inspect and repair services. "
    "Call exactly one tool when a tool can answer the request."
)


@pytest.fixture
def live_provider() -> OpenAIProvider:
    if not os.environ.get(MODEL_ENV):
        pytest.skip(f"set {MODEL_ENV} to an inexpensive model to run the live provider proof")
    # The provider path needs no database, so this proof must not require one either: a
    # placeholder DSN keeps `Settings` valid without asking the operator to set one. It is
    # never connected to.
    settings = Settings(database_url=SecretStr("postgresql://unused/unused"), env="test")
    provider = build_provider(settings, secrets=EnvSecretsProvider())
    assert isinstance(provider, OpenAIProvider), (
        f"set TETHER_MODEL_PROVIDER=openai; configuration selected {type(provider).__name__}"
    )
    return provider


async def test_t11_live_a_configuration_only_switch_reaches_openai(
    live_provider: OpenAIProvider,
) -> None:
    """The provider the worker would build is the real OpenAI adapter, chosen by settings."""
    assert live_provider.name == "openai"
    assert live_provider.capabilities().tool_calling is True


async def test_t11_live_b_a_real_model_proposes_a_neutral_tool_call(
    live_provider: OpenAIProvider,
) -> None:
    history = [
        NeutralMessage(role="system", content=SYSTEM),
        NeutralMessage(role="user", content="Is the checkout service healthy?"),
    ]
    turn = await complete(live_provider, history, MANIFEST)

    assert turn.tool_calls, f"the model answered without a tool call: {turn.text!r}"
    call = turn.tool_calls[0]
    assert type(call) is ToolCallProposal
    assert call.tool in {str(tool["name"]) for tool in MANIFEST}
    assert call.call_id
    assert isinstance(call.arguments, dict)
    # The model chose the arguments; Tether still validates and governs them downstream.
    assert call.arguments.get("service")

    assert turn.usage is not None, "the live provider must report authoritative usage"
    assert turn.usage.input_tokens is not None
    assert turn.usage.input_tokens > 0
    assert turn.usage.output_tokens is not None
    assert turn.usage.output_tokens > 0
    assert_usage_is_honest(turn)


async def test_t11_live_c_a_tool_result_continues_the_turn(
    live_provider: OpenAIProvider,
) -> None:
    """A governed outcome goes back as neutral history and the model continues normally."""
    history = [
        NeutralMessage(role="system", content=SYSTEM),
        NeutralMessage(role="user", content="Is the checkout service healthy?"),
        NeutralMessage(
            role="assistant",
            tool_calls=(
                ToolCallProposal(
                    call_id="call_live_1",
                    tool="get_service_health",
                    arguments={"service": "checkout"},
                ),
            ),
        ),
        NeutralMessage(
            role="tool",
            call_id="call_live_1",
            outcome="applied",
            content='{"service": "checkout", "status": "healthy"}',
        ),
        NeutralMessage(role="user", content="Summarise in one short sentence. Do not call tools."),
    ]
    turn = await complete(live_provider, history, MANIFEST)
    assert turn.text.strip(), "the model produced no final answer after the tool result"
    assert_neutral_turn(turn, TurnSpec(text=turn.text))


async def test_t11_live_d_the_key_never_appears_in_a_log_line(
    live_provider: OpenAIProvider, raw_log: io.StringIO
) -> None:
    """The live path uses the production log pipeline; the credential must not survive it."""
    key = os.environ[LIVE_KEY_ENV]
    history = [
        NeutralMessage(role="system", content=SYSTEM),
        NeutralMessage(role="user", content="Say OK."),
    ]
    await complete(live_provider, history, [])
    assert key not in raw_log.getvalue()
