"""Shared machinery for the provider contract suite (PRD T-11, ADR-0016, AD-06).

The suite states what *any* ``ModelProvider`` must do, so a future adapter can be proven against
it without touching runtime, policy, approval, ledger or workload code. Each provider is
presented through a ``ProviderCase``: given a neutral ``TurnSpec``, the case builds a provider
that produces exactly that turn, plus the history to send it. Nothing in the contract assertions
mentions a provider wire format.

The OpenAI case runs against a mocked HTTP transport, so the deterministic suite never calls the
live API and needs no credentials. The live variant is ``test_T11_live_openai.py`` (marker
``live``), which reuses the assertions below.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from tether.providers.history import ModelTurn, NeutralMessage
from tether.providers.openai_provider import OpenAIProvider
from tether.providers.protocol import ModelProvider
from tether.secrets.provider import SecretValue
from tether.testing.scripted import ScriptedProvider

LIVE_KEY_ENV = "TETHER_OPENAI_API_KEY"
# Not a credential: a syntactically plausible value used only by the mocked transport.
FAKE_KEY = "sk-test-not-a-real-key-000000000000"


@dataclass(frozen=True)
class TurnSpec:
    """One model turn, described in Tether terms rather than any provider format."""

    text: str = ""
    # (tool name, arguments) in the order the model proposed them.
    calls: tuple[tuple[str, dict[str, Any]], ...] = ()
    # (input_tokens, output_tokens) when the provider reports authoritative usage.
    usage: tuple[int, int] | None = None


@dataclass(frozen=True)
class ProviderCase:
    """A provider under contract test, plus how to make it produce a given turn."""

    name: str
    build: Callable[[TurnSpec], tuple[ModelProvider, list[NeutralMessage]]]
    # True when this provider reports authoritative token usage.
    reports_usage: bool


MANIFEST: list[dict[str, Any]] = [
    {
        "name": "get_service_health",
        "description": "Read the health of one service.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    },
    {
        "name": "restart_service",
        "description": "Restart one service.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    },
]


# --------------------------------------------------------------------------------------------
# Scripted provider case
# --------------------------------------------------------------------------------------------


def scripted_case(tmp_path: Path) -> ProviderCase:
    def build(spec: TurnSpec) -> tuple[ModelProvider, list[NeutralMessage]]:
        turn: dict[str, Any] = (
            {"final": spec.text}
            if not spec.calls
            else {"tool_calls": [{"tool": t, "arguments": a} for t, a in spec.calls]}
        )
        # The scripted provider picks its turn by counting assistant messages already in
        # history, so a case that prepends a governed exchange asks for a later turn. Repeating
        # the same turn keeps that determinism while letting one spec answer any position.
        scenario = {"name": "contract", "turns": [turn] * 4}
        (tmp_path / "contract.yaml").write_text(json.dumps(scenario), encoding="utf-8")
        provider = ScriptedProvider(tmp_path, env="test")
        return provider, [NeutralMessage(role="user", content="go #scenario=contract")]

    return ProviderCase(name="scripted", build=build, reports_usage=False)


# --------------------------------------------------------------------------------------------
# OpenAI provider case, over a mocked transport
# --------------------------------------------------------------------------------------------


def openai_response_body(spec: TurnSpec) -> dict[str, Any]:
    """The OpenAI Responses-API body that corresponds to ``spec``."""
    output: list[dict[str, Any]] = []
    if spec.text:
        output.append(
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": spec.text, "annotations": []}],
            }
        )
    for index, (tool, arguments) in enumerate(spec.calls):
        output.append(
            {
                "id": f"fc_{index}",
                "type": "function_call",
                "call_id": f"call_{index}",
                "name": tool,
                "arguments": json.dumps(arguments),
            }
        )
    body: dict[str, Any] = {"id": "resp_1", "object": "response", "output": output}
    if spec.usage is not None:
        body["usage"] = {
            "input_tokens": spec.usage[0],
            "output_tokens": spec.usage[1],
            "total_tokens": spec.usage[0] + spec.usage[1],
        }
    return body


@dataclass
class Recorder:
    """Captures what the adapter put on the wire, so tests can assert the translation."""

    requests: list[httpx2.Request] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)

    def transport(
        self, respond: Callable[[httpx2.Request], httpx2.Response]
    ) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            self.payloads.append(json.loads(request.content.decode("utf-8")))
            return respond(request)

        return httpx2.MockTransport(handle)

    @property
    def payload(self) -> dict[str, Any]:
        assert self.payloads, "the adapter sent no request"
        return self.payloads[-1]

    @property
    def request(self) -> httpx2.Request:
        assert self.requests, "the adapter sent no request"
        return self.requests[-1]


def openai_provider(
    respond: Callable[[httpx2.Request], httpx2.Response],
    *,
    recorder: Recorder | None = None,
    model: str = "test-model",
    api_key: str = FAKE_KEY,
    timeout_seconds: float = 60.0,
    base_url: str = "https://api.openai.test/v1",
) -> OpenAIProvider:
    """The real adapter and the real SDK over a mocked transport: no network, no credential."""
    return OpenAIProvider(
        api_key=SecretValue(api_key),
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        http_client=httpx2.AsyncClient(transport=(recorder or Recorder()).transport(respond)),
    )


def ok(body: dict[str, Any]) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda _request: httpx2.Response(200, json=body)


def openai_case() -> ProviderCase:
    def build(spec: TurnSpec) -> tuple[ModelProvider, list[NeutralMessage]]:
        provider = openai_provider(ok(openai_response_body(spec)))
        return provider, [NeutralMessage(role="user", content="go")]

    return ProviderCase(name="openai", build=build, reports_usage=True)


# --------------------------------------------------------------------------------------------
# Shared assertions, reused by the deterministic and the live suites
# --------------------------------------------------------------------------------------------


def assert_usage_is_honest(turn: ModelTurn) -> None:
    """Usage is absent or authoritative; it is never estimated, and never negative."""
    if turn.usage is None:
        return
    for value in (turn.usage.input_tokens, turn.usage.output_tokens):
        assert value is None or (isinstance(value, int) and value >= 0)


def assert_neutral_turn(turn: ModelTurn, spec: TurnSpec) -> None:
    """``turn`` is Tether's neutral representation of ``spec``, whatever produced it."""
    assert isinstance(turn, ModelTurn)
    assert [(call.tool, call.arguments) for call in turn.tool_calls] == list(spec.calls)
    call_ids = [call.call_id for call in turn.tool_calls]
    assert all(call_ids), "every proposal needs a call id to bind its result to"
    assert len(set(call_ids)) == len(call_ids), "call ids must be unique within a turn"
    if not spec.calls:
        assert turn.text == spec.text
    assert_usage_is_honest(turn)


async def complete(
    provider: ModelProvider,
    history: Sequence[NeutralMessage],
    manifest: Sequence[dict[str, Any]] | None = None,
) -> ModelTurn:
    return await provider.complete(history, MANIFEST if manifest is None else manifest)
