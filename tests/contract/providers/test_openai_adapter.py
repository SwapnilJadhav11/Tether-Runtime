"""The OpenAI adapter: request translation, response translation, errors, credential safety.

These are the only tests in the suite allowed to know OpenAI's wire format. Everything the
runtime sees is asserted by ``test_T11_provider_contract.py`` instead.

Wire format verified against OpenAI's published Responses-API documentation on 2026-11-14:
``POST /v1/responses``; flat function tools (``type``/``name``/``description``/``parameters``);
``function_call`` output items carrying ``call_id``/``name``/``arguments`` (a JSON string);
``function_call_output`` input items carrying ``call_id``/``output``; usage reported as
``input_tokens``/``output_tokens``/``total_tokens``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import httpx2
import openai
import pytest
from tests.contract.providers.harness import (
    FAKE_KEY,
    MANIFEST,
    Recorder,
    TurnSpec,
    complete,
    ok,
    openai_provider,
    openai_response_body,
)

from tether.observability import logging as tether_logging
from tether.providers.history import NeutralMessage, ToolCallProposal
from tether.providers.openai_provider import OpenAIProvider
from tether.providers.protocol import (
    MalformedToolCallError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from tether.secrets.provider import SecretValue

pytestmark = pytest.mark.asyncio

HISTORY = [
    NeutralMessage(role="system", content="You are an operator."),
    NeutralMessage(role="user", content="checkout looks unhealthy"),
]


# --------------------------------------------------------------------------------------------
# Request translation
# --------------------------------------------------------------------------------------------


async def _sent(history: list[NeutralMessage], manifest: Any = None) -> dict[str, Any]:
    recorder = Recorder()
    provider = openai_provider(
        ok(openai_response_body(TurnSpec(text="ok"))), recorder=recorder, model="probe-model"
    )
    await complete(provider, history, MANIFEST if manifest is None else manifest)
    return recorder.payload


async def test_the_request_goes_to_the_responses_endpoint() -> None:
    recorder = Recorder()
    provider = openai_provider(ok(openai_response_body(TurnSpec(text="ok"))), recorder=recorder)
    await complete(provider, HISTORY)
    assert recorder.request.url.path.endswith("/responses")
    assert recorder.request.method == "POST"


async def test_the_configured_model_is_sent() -> None:
    assert (await _sent(HISTORY))["model"] == "probe-model"


async def test_parallel_tool_calls_are_disabled() -> None:
    """Plan W9.1 and ADR-0012: Tether governs one call at a time, so it does not ask for more."""
    assert (await _sent(HISTORY))["parallel_tool_calls"] is False


async def test_the_request_is_stateless() -> None:
    """Tether owns the conversation record; the provider must not retain it."""
    assert (await _sent(HISTORY))["store"] is False


async def test_system_and_user_messages_become_input_items() -> None:
    payload = await _sent(HISTORY)
    assert payload["input"] == [
        {"role": "system", "content": "You are an operator."},
        {"role": "user", "content": "checkout looks unhealthy"},
    ]


async def test_the_manifest_becomes_flat_function_tools() -> None:
    payload = await _sent(HISTORY)
    assert payload["tools"] == [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
            "strict": False,
        }
        for tool in MANIFEST
    ]


async def test_no_tools_key_is_sent_when_no_tool_is_offered() -> None:
    payload = await _sent(HISTORY, manifest=[])
    assert "tools" not in payload
    assert "parallel_tool_calls" not in payload


async def test_an_assistant_tool_call_and_its_result_become_the_paired_items() -> None:
    history = [
        *HISTORY,
        NeutralMessage(
            role="assistant",
            content="restarting",
            tool_calls=(
                ToolCallProposal(
                    call_id="call_7", tool="restart_service", arguments={"service": "checkout"}
                ),
            ),
        ),
        NeutralMessage(
            role="tool", call_id="call_7", outcome="applied", content='{"restarted": true}'
        ),
    ]
    payload = await _sent(history)
    assert payload["input"][2:] == [
        {"role": "assistant", "content": "restarting"},
        {
            "type": "function_call",
            "call_id": "call_7",
            "name": "restart_service",
            "arguments": json.dumps({"service": "checkout"}),
        },
        {"type": "function_call_output", "call_id": "call_7", "output": '{"restarted": true}'},
    ]


async def test_a_denied_call_still_sends_its_outcome_back() -> None:
    """Every governed outcome reaches the model, so it can react instead of repeating itself."""
    history = [
        *HISTORY,
        NeutralMessage(
            role="assistant",
            tool_calls=(
                ToolCallProposal(
                    call_id="call_9", tool="restart_service", arguments={"service": "x"}
                ),
            ),
        ),
        NeutralMessage(role="tool", call_id="call_9", outcome="denied", content="policy denied"),
    ]
    payload = await _sent(history)
    assert payload["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_9",
        "output": "policy denied",
    }


async def test_a_tool_call_without_its_result_is_refused_rather_than_truncated() -> None:
    """ADR-0016: history is never silently truncated to make a request acceptable."""
    history = [
        *HISTORY,
        NeutralMessage(
            role="assistant",
            tool_calls=(ToolCallProposal(call_id="call_1", tool="restart_service", arguments={}),),
        ),
    ]
    provider = openai_provider(ok(openai_response_body(TurnSpec(text="ok"))))
    with pytest.raises(ProviderRequestError):
        await complete(provider, history)


# --------------------------------------------------------------------------------------------
# Response translation
# --------------------------------------------------------------------------------------------


async def test_several_output_text_parts_are_joined() -> None:
    body = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "part one. "},
                    {"type": "output_text", "text": "part two."},
                ],
            }
        ]
    }
    provider = openai_provider(ok(body))
    turn = await complete(provider, HISTORY)
    assert turn.text == "part one. part two."


async def test_unknown_output_item_types_are_ignored() -> None:
    """A new provider-side item type must not break the run or become a tool proposal."""
    body = {
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        ]
    }
    provider = openai_provider(ok(body))
    turn = await complete(provider, HISTORY)
    assert (turn.text, turn.tool_calls) == ("done", ())


async def test_usage_is_taken_from_the_provider_and_never_estimated() -> None:
    provider = openai_provider(ok(openai_response_body(TurnSpec(text="x", usage=(7, 11)))))
    turn = await complete(provider, HISTORY)
    assert turn.usage is not None
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (7, 11)


async def test_missing_usage_is_reported_as_unknown() -> None:
    provider = openai_provider(ok({"output": [], "usage": None}))
    turn = await complete(provider, HISTORY)
    assert turn.usage is None


async def test_arguments_that_are_not_valid_json_are_a_malformed_call() -> None:
    """Invalid model output is never a provider failure (ADR-0016 rule 4)."""
    body = {
        "output": [
            {
                "type": "function_call",
                "call_id": "call_0",
                "name": "restart_service",
                "arguments": '{"service": "checkout"',
            }
        ]
    }
    provider = openai_provider(ok(body))
    with pytest.raises(MalformedToolCallError) as raised:
        await complete(provider, HISTORY)
    assert raised.value.retryable is False


async def test_arguments_that_are_not_an_object_are_a_malformed_call() -> None:
    body = {
        "output": [
            {
                "type": "function_call",
                "call_id": "call_0",
                "name": "restart_service",
                "arguments": '["checkout"]',
            }
        ]
    }
    provider = openai_provider(ok(body))
    with pytest.raises(MalformedToolCallError):
        await complete(provider, HISTORY)


async def test_a_tool_call_without_a_call_id_is_malformed() -> None:
    """Without a call id the result could not be bound back to the call."""
    body = {
        "output": [
            {"type": "function_call", "name": "restart_service", "arguments": "{}", "call_id": ""}
        ]
    }
    provider = openai_provider(ok(body))
    with pytest.raises(MalformedToolCallError):
        await complete(provider, HISTORY)


async def test_a_response_that_is_not_an_object_is_a_provider_response_error() -> None:
    provider = openai_provider(lambda _r: httpx2.Response(200, content=b"not json"))
    with pytest.raises(ProviderResponseError):
        await complete(provider, HISTORY)


async def test_a_response_without_an_output_array_is_a_provider_response_error() -> None:
    provider = openai_provider(ok({"id": "resp_1"}))
    with pytest.raises(ProviderResponseError):
        await complete(provider, HISTORY)


# --------------------------------------------------------------------------------------------
# Error normalisation (ADR-0016; retry itself belongs to plan step W3.3)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (429, ProviderRateLimitedError, True),
        (500, ProviderUnavailableError, True),
        (502, ProviderUnavailableError, True),
        (503, ProviderUnavailableError, True),
        (401, ProviderAuthError, False),
        (403, ProviderAuthError, False),
        (400, ProviderRequestError, False),
        (404, ProviderRequestError, False),
        (422, ProviderRequestError, False),
    ],
)
async def test_http_failures_map_to_typed_provider_errors(
    status: int, expected: type[ProviderError], retryable: bool
) -> None:
    body = {"error": {"type": "invalid_request_error", "code": "bad_thing"}}
    provider = openai_provider(lambda _r: httpx2.Response(status, json=body))
    with pytest.raises(expected) as raised:
        await complete(provider, HISTORY)
    assert raised.value.retryable is retryable
    assert isinstance(raised.value, ProviderError)


async def test_a_timeout_is_a_retryable_provider_timeout() -> None:
    def raise_timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    provider = openai_provider(raise_timeout)
    with pytest.raises(ProviderTimeoutError) as raised:
        await complete(provider, HISTORY)
    assert raised.value.retryable is True


async def test_a_transport_failure_is_a_retryable_provider_unavailable() -> None:
    def raise_connect(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    provider = openai_provider(raise_connect)
    with pytest.raises(ProviderUnavailableError) as raised:
        await complete(provider, HISTORY)
    assert raised.value.retryable is True


async def test_no_httpx_or_openai_exception_escapes_the_adapter() -> None:
    """Whatever the transport raises, callers only ever see a ProviderError."""

    def explode(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ProtocolError("bad framing", request=request)

    provider = openai_provider(explode)
    with pytest.raises(ProviderError) as raised:
        await complete(provider, HISTORY)
    assert not isinstance(raised.value, httpx2.HTTPError)


async def test_an_error_message_does_not_echo_the_provider_error_text() -> None:
    """Provider error prose can quote the prompt; only status and codes are safe to keep."""
    body = {"error": {"message": "sensitive prompt echo: db_password=hunter2", "code": "x"}}
    provider = openai_provider(lambda _r: httpx2.Response(400, json=body))
    with pytest.raises(ProviderRequestError) as raised:
        await complete(provider, HISTORY)
    assert "hunter2" not in str(raised.value)
    assert "sensitive prompt echo" not in str(raised.value)


# --------------------------------------------------------------------------------------------
# Credential containment (ADR-0017, T-16)
# --------------------------------------------------------------------------------------------


async def test_the_key_is_sent_as_a_bearer_token() -> None:
    recorder = Recorder()
    provider = openai_provider(
        ok(openai_response_body(TurnSpec(text="ok"))), recorder=recorder, api_key=FAKE_KEY
    )
    await complete(provider, HISTORY)
    assert recorder.request.headers["authorization"] == f"Bearer {FAKE_KEY}"


async def test_the_key_is_never_rendered_by_the_provider() -> None:
    provider = OpenAIProvider(api_key=SecretValue(FAKE_KEY), model="m")
    for rendered in (repr(provider), str(provider), repr(provider.capabilities())):
        assert FAKE_KEY not in rendered
    await provider.aclose()


async def test_the_key_is_registered_with_the_log_scrubber() -> None:
    provider = OpenAIProvider(api_key=SecretValue(FAKE_KEY), model="m")
    assert FAKE_KEY not in tether_logging.scrub(f"token={FAKE_KEY}")
    await provider.aclose()


async def test_the_key_never_reaches_an_error_message() -> None:
    provider = openai_provider(
        lambda _r: httpx2.Response(401, json={"error": {"code": "invalid_api_key"}}),
        api_key=FAKE_KEY,
    )
    with pytest.raises(ProviderAuthError) as raised:
        await complete(provider, HISTORY)
    assert FAKE_KEY not in str(raised.value)
    assert FAKE_KEY not in repr(raised.value)


# --------------------------------------------------------------------------------------------
# The SDK boundary: one Tether attempt is exactly one provider attempt
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_a_failed_status_is_never_retried_by_the_sdk(status: int) -> None:
    """The SDK retries 429 and 5xx by default; Tether's retry accounting must own that."""
    recorder = Recorder()
    provider = openai_provider(
        lambda _r: httpx2.Response(status, json={"error": {"code": "x"}}), recorder=recorder
    )
    with pytest.raises(ProviderError):
        await complete(provider, HISTORY)
    assert len(recorder.requests) == 1


async def test_a_timeout_is_never_retried_by_the_sdk() -> None:
    recorder = Recorder()

    def raise_timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    provider = openai_provider(raise_timeout, recorder=recorder)
    with pytest.raises(ProviderTimeoutError):
        await complete(provider, HISTORY)
    assert len(recorder.requests) == 1


async def test_a_connection_failure_is_never_retried_by_the_sdk() -> None:
    recorder = Recorder()

    def raise_connect(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    provider = openai_provider(raise_connect, recorder=recorder)
    with pytest.raises(ProviderUnavailableError):
        await complete(provider, HISTORY)
    assert len(recorder.requests) == 1


async def test_the_sdk_client_is_configured_for_zero_retries() -> None:
    provider = openai_provider(ok(openai_response_body(TurnSpec(text="ok"))))
    assert provider._client.max_retries == 0


async def test_the_timeout_and_base_url_are_configurable() -> None:
    recorder = Recorder()
    provider = openai_provider(
        ok(openai_response_body(TurnSpec(text="ok"))),
        recorder=recorder,
        timeout_seconds=7.0,
        base_url="https://gateway.example.test/v1",
    )
    await complete(provider, HISTORY)
    assert recorder.request.url.host == "gateway.example.test"
    assert recorder.request.url.path == "/v1/responses"
    assert provider._client.timeout == 7.0
    assert provider.base_url == "https://gateway.example.test/v1"


def _sdk_request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.openai.test/v1/responses")


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    return cls(
        "prompt echo: db_password=hunter2",
        response=httpx2.Response(status, request=_sdk_request()),
        body={"type": "t", "code": "c", "message": "prompt echo: db_password=hunter2"},
    )


@pytest.mark.parametrize(
    ("raised", "expected", "retryable"),
    [
        (lambda: _status_error(openai.RateLimitError, 429), ProviderRateLimitedError, True),
        (lambda: _status_error(openai.InternalServerError, 500), ProviderUnavailableError, True),
        (lambda: _status_error(openai.AuthenticationError, 401), ProviderAuthError, False),
        (lambda: _status_error(openai.PermissionDeniedError, 403), ProviderAuthError, False),
        (lambda: _status_error(openai.BadRequestError, 400), ProviderRequestError, False),
        (lambda: openai.APITimeoutError(request=_sdk_request()), ProviderTimeoutError, True),
        (
            lambda: openai.APIConnectionError(message="hunter2", request=_sdk_request()),
            ProviderUnavailableError,
            True,
        ),
        (lambda: openai.OpenAIError("prompt echo: db_password=hunter2"), ProviderError, False),
    ],
)
async def test_every_sdk_exception_is_normalised_and_none_escapes(
    raised: Any,
    expected: type[ProviderError],
    retryable: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped at the SDK boundary itself, with the SDK's own exception types."""
    provider = openai_provider(ok(openai_response_body(TurnSpec(text="ok"))))
    monkeypatch.setattr(provider._client.responses, "create", AsyncMock(side_effect=raised()))
    with pytest.raises(ProviderError) as caught:
        await complete(provider, HISTORY)
    error = caught.value
    assert type(error) is expected
    assert error.retryable is retryable
    # No SDK or transport type escapes, and no SDK exception is chained behind the new one.
    assert not isinstance(error, openai.OpenAIError)
    assert type(error).__module__.startswith("tether.")
    assert error.__cause__ is None
    # Provider prose can quote the request, so it never reaches a Tether exception.
    assert "hunter2" not in str(error)
    assert "prompt echo" not in str(error)


async def test_the_sdk_and_its_transport_log_quietly(raw_log: Any) -> None:
    """The SDK's debug logging includes request bodies, which hold prompts."""
    # Debug logging would otherwise be one environment variable away.
    logging.getLogger("openai").setLevel(logging.DEBUG)
    provider = openai_provider(ok(openai_response_body(TurnSpec(text="ok"))))
    for name in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
    await provider.aclose()
