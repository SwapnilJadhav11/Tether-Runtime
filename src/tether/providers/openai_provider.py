"""The OpenAI adapter: the first real hosted provider (TDR-004, D-5, plan W9.1).

This module is the only place in Tether that imports the OpenAI SDK or knows OpenAI's wire
format. It translates neutral history and the tool manifest into one Responses-API call and
translates the answer back into a neutral ``ModelTurn``. Everything outside it sees only
``tether.providers`` types, so switching provider is a configuration change (ADR-0016 rule 6),
and no SDK object, exception or type crosses this module's boundary.

Wire format verified against OpenAI's published Responses-API documentation on 2026-11-14:

* function tools are flat: ``{type, name, description, parameters, strict}``;
* ``parallel_tool_calls: false`` permits at most one tool call per turn;
* a proposed call arrives as an ``output`` item ``{type: "function_call", call_id, name,
  arguments}`` whose ``arguments`` is a JSON-encoded string;
* a result goes back as an ``input`` item ``{type: "function_call_output", call_id, output}``;
* assistant prose arrives as ``{type: "message", content: [{type: "output_text", text}]}``;
* usage is ``{input_tokens, output_tokens, total_tokens}``.

Deliberate choices:

* **The official SDK, with retries off.** The SDK retries 429 and 5xx by default (two retries).
  Tether counts and records provider attempts, so one Tether attempt must be exactly one request
  on the wire: ``max_retries=0``. Bounded, stop-gated retry is plan step W3.3 and belongs to
  Tether, where each attempt is recorded and the stop predicate is checked before it.
* **The SDK's objects are read defensively.** Fields are read through ``_get`` rather than
  trusted, because a malformed or unexpected response must become a typed error, not an
  ``AttributeError`` that fails the worker.
* **``store: false``.** Tether owns the conversation record; the provider must not retain it.
* **``strict: false``.** Tool schemas come from workload-authored Pydantic models, which need not
  satisfy OpenAI's strict-mode restrictions. Tether validates every proposal against the tool's
  input model itself, so provider-side validation is never relied on (ADR-0009, ADR-0012).
* **Provider error prose is never kept.** An SDK exception's message can quote the request, so
  only the status and the provider's short ``type``/``code`` survive into a Tether exception.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

import openai

from tether.observability.logging import register_secret
from tether.providers.history import ModelTurn, ModelUsage, NeutralMessage, ToolCallProposal
from tether.providers.protocol import (
    MalformedToolCallError,
    ProviderAuthError,
    ProviderCapabilities,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from tether.secrets.provider import SecretValue

if TYPE_CHECKING:
    import httpx2

DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_SECONDS: Final = 60.0
# One Tether attempt is one request on the wire; the SDK must never retry behind Tether's back.
_SDK_MAX_RETRIES: Final = 0


# `raise ... from None` throughout: an SDK or transport exception can carry the request, and so
# the prompt, in its representation and traceback. Tether keeps the normalised error only
# (ADR-0017), which is also how `tether.testing.scripted` suppresses its causes.


class OpenAIProvider:
    """A ``ModelProvider`` backed by OpenAI's Responses API through the official SDK."""

    name = "openai"

    __slots__ = ("_client", "base_url", "model")

    def __init__(
        self,
        *,
        api_key: SecretValue[str],
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        # The key is a process-local secret: registered with the log and trace scrubber so it
        # cannot survive a log line, and otherwise held only inside the SDK client.
        register_secret(api_key.reveal())
        # The SDK's own DEBUG logging includes request bodies, which hold prompts, and its
        # `OPENAI_LOG` variable can switch it on. Vendor knowledge stays in this adapter,
        # so the SDK's logger is held at WARNING here rather than in shared logging code.
        logging.getLogger("openai").setLevel(logging.WARNING)
        self._client = openai.AsyncOpenAI(
            api_key=api_key.reveal(),
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=_SDK_MAX_RETRIES,
            # Only tests pass one, to drive the real SDK over a mocked transport.
            http_client=http_client,
        )

    def __repr__(self) -> str:
        return f"OpenAIProvider(model={self.model!r}, base_url={self.base_url!r})"

    async def aclose(self) -> None:
        await self._client.close()

    def capabilities(self) -> ProviderCapabilities:
        # The context window is model-specific and this adapter keeps no model table, so it is
        # reported as unknown rather than guessed (see ProviderCapabilities).
        return ProviderCapabilities(
            tool_calling=True,
            parallel_tool_calls_controllable=True,
            context_window_tokens=None,
        )

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        request = self._request(history, manifest)
        try:
            response = await self._client.responses.create(**request)
        except openai.APITimeoutError:
            raise ProviderTimeoutError("the model provider timed out") from None
        except openai.APIConnectionError:
            raise ProviderUnavailableError("the model provider is unreachable") from None
        except openai.APIStatusError as error:
            raise _status_error(error) from None
        except openai.OpenAIError:
            raise ProviderError("the model provider call failed") from None
        return self._turn(response)

    # ----------------------------------------------------------------------------------------
    # Request translation
    # ----------------------------------------------------------------------------------------

    def _request(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "input": _render_input(history),
            "store": False,
        }
        if manifest:
            request["tools"] = [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                    "strict": False,
                }
                for tool in manifest
            ]
            # ADR-0012 governs one call at a time, so Tether never asks for parallel calls.
            request["parallel_tool_calls"] = False
        return request

    # ----------------------------------------------------------------------------------------
    # Response translation
    # ----------------------------------------------------------------------------------------

    def _turn(self, response: Any) -> ModelTurn:
        output = _get(response, "output")
        if not isinstance(output, list):
            raise ProviderResponseError("the response has no output array")
        texts: list[str] = []
        calls: list[ToolCallProposal] = []
        for item in output:
            kind = _get(item, "type")
            if kind == "message":
                texts.extend(_output_text(item))
            elif kind == "function_call":
                calls.append(_proposal(item))
            elif kind is None:
                raise ProviderResponseError("an output item has no type")
            # Any other item type (reasoning summaries and the like) carries nothing Tether
            # governs, so it is ignored rather than guessed at.
        return ModelTurn(text="".join(texts), tool_calls=tuple(calls), usage=_usage(response))


# --------------------------------------------------------------------------------------------
# Translation helpers
# --------------------------------------------------------------------------------------------


def _get(obj: Any, name: str) -> Any:
    """A field of an SDK model, a plain mapping, or nothing: never an ``AttributeError``."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _render_input(history: Sequence[NeutralMessage]) -> list[dict[str, Any]]:
    """Neutral history as Responses-API input items.

    Each proposed call is emitted immediately followed by the result that answers it, because the
    API requires every ``function_call_output`` to match a ``function_call`` in the same request.
    Nothing is dropped: a call whose result is missing, or a result with no call, is a typed
    failure, since ADR-0016 forbids trimming history to make a request acceptable.
    """
    results = {
        message.call_id: message
        for message in history
        if message.role == "tool" and message.call_id
    }
    answered: set[str] = set()
    items: list[dict[str, Any]] = []
    for message in history:
        if message.role == "tool":
            continue  # emitted beside the call it answers
        if message.role in ("system", "user"):
            items.append({"role": message.role, "content": message.content})
            continue
        if message.content:
            items.append({"role": "assistant", "content": message.content})
        for call in message.tool_calls:
            result = results.get(call.call_id)
            if result is None:
                raise ProviderRequestError(
                    "a proposed tool call has no recorded result, so the history cannot be "
                    "rendered without dropping it"
                )
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.tool,
                    "arguments": json.dumps(call.arguments),
                }
            )
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result.content,
                }
            )
            answered.add(call.call_id)
    if set(results) - answered:
        raise ProviderRequestError("a tool result refers to no proposed call in this history")
    return items


def _output_text(item: Any) -> list[str]:
    content = _get(item, "content")
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for part in content:
        text = _get(part, "text")
        if _get(part, "type") == "output_text" and isinstance(text, str):
            texts.append(text)
    return texts


def _proposal(item: Any) -> ToolCallProposal:
    call_id = _get(item, "call_id")
    name = _get(item, "name")
    raw = _get(item, "arguments")
    if not isinstance(call_id, str) or not call_id:
        raise MalformedToolCallError("a proposed tool call has no call id")
    if not isinstance(name, str) or not name:
        raise MalformedToolCallError("a proposed tool call has no tool name")
    if not isinstance(raw, str):
        raise MalformedToolCallError(f"the arguments of {name!r} are not a JSON string")
    try:
        arguments = json.loads(raw)
    except ValueError:
        # The offending text is never echoed: it is model output and may carry anything.
        raise MalformedToolCallError(f"the arguments of {name!r} are not valid JSON") from None
    if not isinstance(arguments, dict):
        raise MalformedToolCallError(f"the arguments of {name!r} are not a JSON object")
    return ToolCallProposal(call_id=call_id, tool=name, arguments=arguments)


def _usage(response: Any) -> ModelUsage | None:
    usage = _get(response, "usage")
    if usage is None:
        return None
    return ModelUsage(
        input_tokens=_token_count(_get(usage, "input_tokens")),
        output_tokens=_token_count(_get(usage, "output_tokens")),
    )


def _token_count(value: Any) -> int | None:
    # `bool` is an `int` in Python; a boolean token count would be nonsense, so it is rejected.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _status_error(error: openai.APIStatusError) -> ProviderError:
    """Map an SDK status error to a typed provider error.

    Only the status and the provider's short error ``type``/``code`` are kept. The SDK exception
    message is the provider's own prose and can quote the request, which may contain prompt or
    tool content, so it is never copied into a Tether exception (the rule in
    `tether.core.errors`).
    """
    status = error.status_code
    label = _error_label(error)
    if status == 429:
        return ProviderRateLimitedError(f"the model provider rate-limited the request{label}")
    if status in (401, 403):
        return ProviderAuthError(f"the model provider rejected the credential{label}")
    if status >= 500:
        return ProviderUnavailableError(f"the model provider returned {status}{label}")
    return ProviderRequestError(f"the model provider rejected the request with {status}{label}")


def _error_label(error: openai.APIStatusError) -> str:
    parts = [
        value
        for value in (getattr(error, "type", None), getattr(error, "code", None))
        if isinstance(value, str) and value
    ]
    return f" ({'/'.join(parts)})" if parts else ""
