# Adding a model provider

This guide is for engineers who add a model provider adapter to Tether. A provider turns Tether's provider-neutral conversation into one vendor's API call and back. The adapter is the only code that knows the vendor; governance, policy, approvals, the ledger and workloads never do. Two providers exist: the scripted provider (development and test only) and the OpenAI adapter, which this page uses as the worked example. Operating a configured provider is covered in [model providers](../operations/model-providers.md).

Provider failover is designed ([ADR-0016](../adr/0016-provider-neutral-history-and-fallback.md)) and not implemented. One provider serves a deployment, chosen by configuration.

## The boundary

The runtime holds a `ModelProvider` (`src/tether/providers/protocol.py`) and nothing more:

```python
class ModelProvider(Protocol):
    name: str
    model: str

    def capabilities(self) -> ProviderCapabilities: ...

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn: ...
```

`complete` takes Tether's history and the tool manifest, makes **one** model call, and returns one `ModelTurn`. The manifest is a list of `{name, description, input_schema}` rendered from the agent's tools.

Behind this line, and only there: the vendor's SDK and HTTP client, its wire format, its error shapes, its credential and its name. A test, `tests/contract/providers/test_provider_boundary.py`, keeps it true. It scans the core with three separate path allow-lists. The vendor's name may appear only in the adapter, the factory and `settings.py`. The vendor SDK import and the HTTP-client import may appear only in the adapter, not even in the factory. In addition, the run graph and driver must not mention the concrete adapter module and must import the protocol. A new provider therefore extends the vendor-name pattern (the test matches `openai` only), and adds its adapter to the SDK and HTTP-client allow-lists, in the same reviewed change. Do not loosen the guard to make a leak pass.

## The neutral types

Defined in `src/tether/providers/history.py`. All are frozen and reject unknown fields.

| Type | Fields | Notes |
|---|---|---|
| `NeutralMessage` | `role` (`system`, `user`, `assistant`, `tool`), `content`, `tool_calls`, `call_id`, `outcome` | A `tool` message answers one call and carries a neutral `CallOutcome`. Payloads in history are already masked. |
| `ToolCallProposal` | `call_id`, `tool`, `arguments`, `malformed` | The model's request. Untrusted: Tether validates and governs it. |
| `ModelTurn` | `text`, `tool_calls`, `usage` | A final answer when `tool_calls` is empty. |
| `ModelUsage` | `input_tokens`, `output_tokens` | Each `None` if the provider did not report it. |

`CallOutcome` values are `applied`, `not_applied`, `failed`, `denied`, `invalid`, `rejected`, `expired`, `reconciled` and `skipped`. A provider renders them as tool-result content; it never interprets them.

The history is Tether's record, not the vendor's. The adapter renders it into a request each time and must not retain it at the vendor if the API allows otherwise. It also **must not shorten it**: never drop, truncate or summarise messages to make a request fit. If the history cannot be represented, raise a typed error ([ADR-0016](../adr/0016-provider-neutral-history-and-fallback.md)).

## Capabilities

`capabilities()` returns `ProviderCapabilities(tool_calling, parallel_tool_calls_controllable, context_window_tokens)`. Declare only what you know. The OpenAI adapter reports `context_window_tokens=None` because it keeps no model table. An unknown window can never be shown to fit a history, so a provider that reports `None` could not qualify as a fallback target once fallback exists.

## What the adapter must do

**One Tether attempt is one request on the wire.** Switch off the SDK's own retries. Tether retries a model call itself, bounded and gated on the stop predicate, and records every attempt in `model_calls`. A hidden SDK retry would break the attempt count and the stop check before each attempt.

**Ask for at most one tool call per turn where the API lets you.** Tether governs multiple calls in a turn one at a time, in order ([ADR-0012](../adr/0012-sequential-multi-call-governance.md)), so the adapter must still translate several calls in the order the model proposed them, with a unique `call_id` each. OpenAI is called with `parallel_tool_calls: false`; this shapes the request and is not a safety assumption.

**Map every failure to a Tether error.** No vendor or HTTP exception may escape. `raise ... from None`, because an exception's representation and traceback can contain the request, and so the prompt.

| Condition | Error | `retryable` |
|---|---|---|
| timeout | `ProviderTimeoutError` | yes |
| connection failure, HTTP 5xx | `ProviderUnavailableError` | yes |
| HTTP 429 | `ProviderRateLimitedError` | yes |
| HTTP 401, 403 | `ProviderAuthError` | no |
| other 4xx; a history that cannot be rendered | `ProviderRequestError` | no |
| a response without the documented shape | `ProviderResponseError` | no |
| a tool call with no call id or name, or arguments the API does not document as a string | `MalformedToolCallError` | no |

Each error has a short, label-safe `code`, stored in `model_calls.error_code` and usable as a metric label. Keep only the HTTP status and the vendor's short `type` or `code` in the message. Vendor error prose can quote the request, so it is never copied.

**Treat unparseable arguments as model output, not a provider fault.** If a call's arguments are not a JSON object, return `ToolCallProposal(malformed=True)` with no arguments. The run answers it with a typed `invalid` result inside the repair budget. Drop the raw text: it is never stored, logged or echoed.

**Report usage honestly.** Take the vendor's figures, and use `None` for a field it did not report. Never estimate. Counts must be non-negative integers. Dollar cost is not derived.

**Read the response defensively.** A missing field must become a typed error, not an `AttributeError` that fails the worker. Ignore output item types that Tether does not govern.

**Handle the credential as a secret.** Read it through the `SecretsProvider` (`get(name)` returns a `SecretValue`), not from `Settings`, so it cannot appear in a settings dump or repr. Register its value with the log and trace scrubber (`register_secret`), send it only in the auth header, keep it out of `__repr__`, and hold the vendor SDK's logger at WARNING, because debug logging includes request bodies and those hold prompts.

**Make timeout and base URL configuration**, and leave the model unset by default: Tether never spends on a model nobody chose.

If the adapter holds a connection pool, give it an `aclose()`. The worker calls it at shutdown if it exists.

## Where a provider is selected

`src/tether/providers/factory.py` has one function, `build_provider(settings)`, with one `match` on `settings.provider`. The setting is `Literal["scripted", "openai"]` in `src/tether/settings.py`, read from `TETHER_MODEL_PROVIDER`. It is deliberately not a registry or a plugin system.

To add a provider:

1. Write the adapter module under `src/tether/providers/`.
2. Add its name to the `Literal` in `Settings`, and its own settings (model, base URL, timeout). The credential is not a setting.
3. Add a `case` in `build_provider` that validates the configuration and builds the adapter. A missing required value raises `ProviderConfigurationError`, so the worker fails to start rather than running misconfigured.
4. Name the credential variable as a constant next to the existing `OPENAI_API_KEY_NAME`, and read it with `source.get(...)`.
5. Document the settings in [configuration](../operations/configuration.md) and [model providers](../operations/model-providers.md).

The scripted provider is built only when `TETHER_ENV` is `dev` or `test`, and the production image does not contain it.

## The worked example: the OpenAI adapter

`src/tether/providers/openai_provider.py` is the only module that imports the OpenAI SDK.

- **Request.** It sends one Responses API call. Tools are flat function tools built from the manifest (`type`, `name`, `description`, `parameters`, `strict: false`, because tool schemas come from workload Pydantic models that need not meet strict-mode limits, and Tether validates every proposal itself). It sets `store: false` and `parallel_tool_calls: false`, and `max_retries=0` on the SDK client.
- **History rendering.** System and user messages become input items. An assistant tool call is emitted immediately followed by the result that answers it, because the API requires them paired. A call with no recorded result, or a result with no call, raises `ProviderRequestError` instead of being dropped.
- **Response.** `message` items yield text; `function_call` items yield proposals; other items are ignored. `usage` becomes `ModelUsage`.
- **Errors.** `APITimeoutError` becomes `ProviderTimeoutError`, `APIConnectionError` becomes `ProviderUnavailableError`, and `APIStatusError` is mapped by status as in the table above.
- **Secrets.** The key is a `SecretValue`, registered with the scrubber on construction, never in `__repr__`.

Read `tests/contract/providers/test_openai_adapter.py` alongside it: it asserts each of these behaviours against a mocked transport.

## The contract suite

`tests/contract/providers/` states what any provider must do, without mentioning a wire format. A provider enters the suite as a `ProviderCase` (`harness.py`):

```python
@dataclass(frozen=True)
class ProviderCase:
    name: str
    build: Callable[[TurnSpec], tuple[ModelProvider, list[NeutralMessage]]]
    reports_usage: bool
```

`TurnSpec` describes a turn in Tether terms: text, a sequence of `(tool, arguments)` calls, and optional usage. `build` returns a provider that produces exactly that turn, over a mocked transport, plus a starting history. The `case` fixture in `conftest.py` is parametrised over every provider. The shared tests check that a final answer is neutral text; one call is one proposal; several calls keep their order with unique ids; structured arguments round-trip; a history that already holds a governed exchange is accepted; usage is reported when the vendor knows it; capabilities are declared; the manifest is the only tool source; and only Tether types cross the boundary.

To prove a new adapter:

1. Add a `<vendor>_case()` to `harness.py` that builds the real adapter against a mocked transport, as `openai_case()` does with `httpx2.MockTransport`.
2. Add its name to the `params` of the `case` fixture **and** a matching branch in the fixture body. The body is a two-way conditional (scripted, otherwise OpenAI), so a new name added to `params` alone would silently run the OpenAI case. Make an unknown name raise instead of falling through.
3. Write adapter-specific tests in the style of `test_openai_adapter.py`: the request on the wire, every error mapping, no SDK retries, no secret in any message or log line.

Run the suite. It needs no network and no database:

```bash
uv run poe test-provider-contract
```

CI runs it on Linux and Windows. An integration test, `tests/integration/test_provider_swap.py`, runs one governed task through the scripted provider and then through the OpenAI adapter, selected only by `TETHER_MODEL_PROVIDER`, and asserts that the run status, the audit event sequence and the ledger are identical while the recorded provider differs. That is the evidence that changing provider changes the model and not the governance.

## Live tests

A mocked transport proves your translation, not the vendor. The live tests call the real API:

```bash
export TETHER_MODEL_PROVIDER=openai
export TETHER_OPENAI_API_KEY=<your-key>
export TETHER_OPENAI_MODEL=<an inexpensive model that supports tool calling>
uv run poe test-live
```

They carry the `live` marker, skip without a key, and run the shared assertions plus one governed incident-remediation run, approved by a second identity, on the real model. They send only a synthetic operations prompt and a small tool manifest. They are **manual and never part of CI**, for four reasons: they cost money, they need a credential that must not live in a CI job, a hosted model is not deterministic, and the vendor's availability is not a property of the change under review. The live proof was run once on one development host; see [validation](../validation.md). Add a live variant for a new provider in the same shape, and keep it opt-in.

## Checklist

- [ ] The vendor's SDK, HTTP client and name appear only in the adapter and the factory; the boundary test passes.
- [ ] One request per `complete` call; SDK retries are off.
- [ ] Every failure is a Tether error with the right `retryable` flag; none carries vendor text.
- [ ] Malformed arguments become `malformed=True`; missing id or name is `MalformedToolCallError`.
- [ ] Usage is reported as the vendor gave it, or `None`.
- [ ] The history is never shortened; an unrenderable history is a typed error.
- [ ] The credential comes from the `SecretsProvider`, is registered with the scrubber, and appears in no repr, log or message.
- [ ] No default model; configuration errors stop startup.
- [ ] The provider is a `ProviderCase`, and the contract suite passes.
- [ ] A live test exists, is opt-in, and is documented.

## Related

- [Model providers](../operations/model-providers.md)
- [Run lifecycle](../architecture/run-lifecycle.md)
- [Secrets and data handling](../security/secrets-and-data-handling.md)
- [Testing](testing.md)
