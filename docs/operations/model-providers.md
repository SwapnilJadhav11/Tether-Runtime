# Model providers: configuration and credentials

> **Status:** written for the provider work (plan W9.1, with the configuration-selection part of
> W3.3), now merged. It documents only what is implemented. It is not a guarantee
> statement: `docs/product/tether-v1-guarantees.md` remains the authority on what is proven.

Tether chooses its model provider from configuration alone (ADR-0016 rule 6). The run graph,
policy engine, approval path, ledger, worker and workloads hold a `ModelProvider` and never name
a vendor, so switching provider changes no code.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `TETHER_MODEL_PROVIDER` | `scripted` | `scripted` or `openai`. Anything else fails startup. |
| `TETHER_OPENAI_API_KEY` | — | **Secret.** Read through the `SecretsProvider`, never a settings field. Required when the provider is `openai`. |
| `TETHER_OPENAI_MODEL` | *none* | Required when the provider is `openai`. There is deliberately no default: Tether never spends on a model nobody chose. |
| `TETHER_OPENAI_BASE_URL` | `https://api.openai.com/v1` | For a proxy or an OpenAI-compatible gateway. |
| `TETHER_OPENAI_TIMEOUT_SECONDS` | `60` | Upper bound on one model call. |
| `TETHER_PROVIDER_MAX_ATTEMPTS` | `3` | Provider attempts per model turn, retries included (W3.3). Must be positive. |
| `TETHER_MAX_REPAIRS`, `TETHER_MAX_DENIALS` | `3`, `3` | The repair/denial bound per run (W3.4, PD-15). Must be positive. |

`TETHER_ENV` still governs the scripted provider: since W7.1 it loads only when `TETHER_ENV` is
`dev` or `test` (an allow-list; `prod`, unset and misspelt values are refused), and `TETHER_ENV`
itself has no default.

**Run against the real API once (2026-11-17).** With the owner's confirmation (model `gpt-4o-mini`, a 5 USD ceiling) `uv run poe test-live` passed and governed runs completed on the compose stack. In CI the adapter is still exercised only against a mocked
transport and a local stub; the live tests (`uv run poe test-live`) skip without credentials
and stay manual; any further paid call needs the owner's confirmation of the model and a spend
limit.

The compose stack (`deploy/compose/docker-compose.yml`) passes `TETHER_MODEL_PROVIDER`,
`TETHER_OPENAI_MODEL` and `TETHER_OPENAI_API_KEY` from `deploy/compose/.env` to `api` and
`worker`. Left unset, the provider is `scripted` and the stack behaves as before. To run the
stack on OpenAI, set all three in `.env` (git-ignored; `.env.example` holds empty placeholders
only).

## Credential handling

The key is supplied by the operator through the environment and reaches the adapter as a
`SecretValue`. It is **not** a `Settings` field, so it cannot appear in a settings dump or repr.
On construction the adapter registers the key with the log and trace scrubber, so a value that
ever reaches a log line is replaced. The adapter sends it only as an `Authorization: Bearer`
header, and no provider error message is copied into a Tether exception, because provider error
prose can quote the request.

Never put the key in `deploy/compose/.env.example`, bootstrap YAML, a fixture or a test
snapshot. Bootstrap configuration holds public keys only (ADR-0014).

## What the OpenAI adapter does

`src/tether/providers/openai_provider.py` is the only module that imports the official OpenAI
Python SDK or knows OpenAI's wire format. It makes one Responses-API call per model call and
translates the answer into a neutral `ModelTurn`. No SDK object, exception or type leaves that
module, and a test fails if the SDK is imported anywhere else in the core.

- **SDK automatic retries are off (`max_retries=0`).** The SDK retries 429 and 5xx twice by
  default. Tether records every provider attempt, so one Tether attempt must be exactly one
  request on the wire; a hidden retry would make attempt accounting and the stop-predicate check
  before each attempt untrue. Tests prove one request per failure for 429, 5xx, timeout and
  connection errors.
- **Timeout and base URL** are configuration (`TETHER_OPENAI_TIMEOUT_SECONDS`,
  `TETHER_OPENAI_BASE_URL`) and are passed to the SDK client.
- **The SDK's own logging is held at WARNING.** Its DEBUG output includes request bodies, which
  hold prompts, and an `OPENAI_LOG` environment variable could switch it on.

- **`parallel_tool_calls: false`** — ADR-0012 governs one call at a time, so Tether never asks
  for parallel calls. A multi-call response would still translate correctly and be governed in
  sequence, so this is a request-shaping choice, not a safety assumption.
- **`store: false`** — Tether owns the conversation record; the provider does not retain it.
- **`strict: false`** on tool schemas — they come from workload-authored Pydantic models, which
  need not satisfy OpenAI's strict-mode restrictions. Tether validates every proposal against
  the tool's input model itself and never relies on provider-side validation.
- **Usage** is taken from the provider's `usage` object and never estimated. A field the provider
  does not report stays `None`. Dollar cost is deferred (D-7); no price is derived.
- **No retry inside the adapter, and no fallback.** A failure is classified and reported;
  `ProviderError.retryable` says whether another attempt could plausibly succeed. The run graph
  does the retrying (below). Automatic failover is deferred (D-5), designed in ADR-0016 and not
  implemented.

### Bounded, stop-gated retry (W3.3)

The run graph's `call_model` node retries a **retryable** error only (ADR-0016 rule 3), up to
`TETHER_PROVIDER_MAX_ATTEMPTS` attempts per model turn. Between attempts it waits the PD-13
backoff (base 0.5 s, doubling, capped at 5 s, full jitter). Before **every** attempt it takes the
fenced run guard and checks the stop predicate (ADR-0006); after every attempt it records the
attempt's row and checks the stop again. A stop committed at any point ends the run
`cancelled`/`timed_out` with no further provider call. A non-retryable error, or the last
attempt failing, ends the run `failed`. After a worker crash mid-sequence the node re-runs from
attempt 1; every attempt still gets its own row and none is deduplicated.

A slow model call no longer outlives its job lease: since W6.1 the worker extends the lease of
the job it is processing with a heartbeat (and the per-run lock and the fencing token still hold).

### Error classification

| Condition | Error | `retryable` |
|---|---|---|
| timeout | `ProviderTimeoutError` | yes |
| connection or protocol failure, HTTP 5xx | `ProviderUnavailableError` | yes |
| HTTP 429 | `ProviderRateLimitedError` | yes |
| HTTP 401, 403 | `ProviderAuthError` | no |
| other 4xx, or a history that cannot be rendered | `ProviderRequestError` | no |
| response shape not as documented | `ProviderResponseError` | no |
| a tool call with no call id or no name, or arguments that are not a string | `MalformedToolCallError` | no |

A history is unrenderable when a proposed call has no recorded result, or a result refers to no
proposed call. ADR-0016 forbids trimming history to make a request acceptable, so this is a typed
failure rather than a silent drop.

### Malformed tool calls and the repair/denial bound (W3.4)

Tool-call arguments that do not parse into a JSON object are *model output*, not a provider
fault (ADR-0016 rule 4). The adapter hands such a call over as a `ToolCallProposal` with
`malformed=True` and no arguments, and drops the raw text: it is never persisted, logged or
audited (ADR-0017). The run graph records it as `CALL_INVALID` with the reason
`arguments: malformed`, and the model gets a typed `invalid` result. When the history is sent
back, the call is rendered with empty arguments (`{}`).

Invalid proposals (unknown tool, schema-invalid or malformed arguments) count as **repairs**;
policy denials (a `deny` decision, or TX5's `policy now: deny`) count as **denials**. Human
rejections and expired approvals count for nothing. The budgets are `TETHER_MAX_REPAIRS` and
`TETHER_MAX_DENIALS` (PD-15: 3 each, cumulative over the run). The occurrence that exceeds a
budget ends the run `failed` with `repair_budget_exhausted` or `denial_budget_exhausted`, as a
terminating result: no further model call, no `CALL_SKIPPED` for the rest of the turn, held in
`awaiting_reconciliation` while any ledger entry is unresolved, and an active cancel or deadline
still decides `cancelled`/`timed_out` with the reason at the end of the summary.

## What is recorded per attempt

Every model attempt writes one `tether.model_calls` row inside the run's existing transaction
(ADR-0016 rule 5):

| Column | Written |
|---|---|
| `provider`, `model`, `turn_ordinal`, `attempt`, `status`, `occurrence_id` | always; `attempt` is the attempt number within the turn, and every attempt has its own `occurrence_id` |
| `input_tokens`, `output_tokens` | the provider's own figures; `NULL` when it reported none |
| `error_code` | on a failed call: the normalised code from the table above |
| `cost_usd` | never — dollar cost is deferred (D-7) |

The provider's own error text is never stored. Only the normalised code is authoritative, because
provider error prose can quote the request.

## Tests

```
uv run poe test-provider-contract   # both providers, mocked transport, no network, no database
uv run poe test-live                # the real API; skips unless a key and model are configured
```

`tests/integration/test_provider_swap.py` runs one governed task twice — scripted, then the
OpenAI adapter — choosing the provider purely through `TETHER_MODEL_PROVIDER` and pointing the
adapter at a local HTTP stub via `TETHER_OPENAI_BASE_URL`. It asserts that the run status, the
audit event sequence with its policy decision, and the ledger entry are *identical* across
providers, while the recorded provider and usage differ. That is the evidence behind the claim
that changing provider configuration changes the model provider and not Tether's governance.

`tests/contract/providers/` holds the shared contract: the same assertions run against the
scripted provider and the OpenAI adapter, so a future adapter is proven by adding a
`ProviderCase` and changing nothing else. `test_provider_boundary.py` greps the core to keep
vendor names and the HTTP client out of the governed layers.

`uv run poe test-live` is manual only and never part of ordinary CI. It needs:

```
TETHER_MODEL_PROVIDER=openai
TETHER_OPENAI_API_KEY=<operator's key>
TETHER_OPENAI_MODEL=<an inexpensive model that supports tool calling>
```

It sends a synthetic operations prompt and a two-tool manifest, nothing from the repository or
any real system, and costs a few hundred tokens. It also runs
`tests/integration/test_live_openai_governed_run.py`: one governed incident-remediation run on
the real model against the in-process demo target, approved by a second identity, asserting
structural facts only (terminal status, complete decision/authorization/dispatch/outcome events
per executed call, a valid audit chain, OpenAI rows with reported tokens). That test needs the
test database (the task starts it) and skips unless the run selected `-m live`, so a key left in
`.env` never makes `poe test-integration` spend. It costs a few model turns.
