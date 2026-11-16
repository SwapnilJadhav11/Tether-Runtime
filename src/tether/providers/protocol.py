"""The model-provider protocol (AD-06, ADR-0016).

Two implementations: the deterministic scripted provider (dev and test only) and one hosted
adapter. The runtime holds a ``ModelProvider`` and nothing more, so which model vendor is
configured is invisible to governance, policy, approval, ledger and workload code — this module
names no vendor, and ``tests/contract/providers/test_provider_boundary.py`` keeps it that way.

Errors are normalised here. An adapter never lets its HTTP client's or SDK's exception types
escape: callers see a ``ProviderError`` whose ``code`` is a short, label-safe string and whose
``retryable`` flag says whether another attempt could plausibly succeed. Bounded retry itself is
plan step W3.3; this module only carries the information a retry would need.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from tether.providers.history import ModelTurn, NeutralMessage


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What an adapter can do (ADR-0016 rule 2), declared honestly.

    ``context_window_tokens`` is ``None`` when the adapter does not know it. A provider with an
    unknown context window can never be shown to fit a rendered history, so it could not qualify
    as a fallback target; capability-qualified fallback is designed in ADR-0016 and deferred.
    """

    tool_calling: bool
    parallel_tool_calls_controllable: bool
    context_window_tokens: int | None = None


class ProviderError(Exception):
    """The model call failed; the run records a failed model call."""

    # Short, stable, label-safe: suitable for `model_calls.error_code` and a metric label.
    code = "provider_error"
    # Whether another attempt could plausibly succeed (ADR-0016 rule 3).
    retryable = False


class ProviderTimeoutError(ProviderError):
    """The provider did not answer within the configured timeout."""

    code = "timeout"
    retryable = True


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached, or answered with a server error."""

    code = "unavailable"
    retryable = True


class ProviderRateLimitedError(ProviderError):
    """The provider refused the request because of rate or quota limits."""

    code = "rate_limited"
    retryable = True


class ProviderAuthError(ProviderError):
    """The credential was rejected. Retrying with the same credential cannot help."""

    code = "auth_failed"


class ProviderRequestError(ProviderError):
    """The provider rejected the request, or Tether cannot represent the history for it.

    Never raised to paper over missing history: ADR-0016 forbids truncating or summarising a
    conversation to make a request acceptable, so an unrepresentable history is a typed failure.
    """

    code = "request_rejected"


class ProviderResponseError(ProviderError):
    """The response did not have the shape the provider's API documents."""

    code = "response_invalid"


class MalformedToolCallError(ProviderError):
    """The model proposed a tool call that cannot be represented as a neutral proposal.

    This is *model output*, not a provider fault, and ADR-0016 rule 4 wants it handled as a typed
    ``invalid`` result inside the repair budget rather than as a failed call. The neutral contract
    cannot yet carry a malformed call (``ToolCallProposal.arguments`` is a ``dict``), so an
    adapter fails closed rather than inventing arguments that might accidentally validate.
    Converting this into a typed ``invalid`` result belongs to the model-output and
    repair-budget work (plan steps W3.4 / S6.1).
    """

    code = "malformed_tool_call"


class ModelProvider(Protocol):
    name: str
    model: str

    def capabilities(self) -> ProviderCapabilities: ...

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn: ...
