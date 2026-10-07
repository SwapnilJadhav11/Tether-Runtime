# ADR-0016: Provider-neutral history with capability-qualified, per-turn fallback

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-06; §13.3 (model step replay); §24 item 6

## Context

The PRD requires:
- switching between Ollama and a hosted provider by configuration only;
- fallback mid-run to a configured, capability-qualified secondary, without ever truncating silently;
- invalid model output that is never treated as a provider failure.

Providers differ in tool-call formats, call-ID conventions, parallel-call control, and context size.

## Decision

1. **Neutral history.** Conversation and tool history live in graph state in a **Tether-owned neutral representation**: system, user, assistant text plus tool calls with call IDs, and typed tool results with an outcome. Only masked payloads enter history. Adapters translate in both directions.
2. **Provider interface.** The provider interface exposes capabilities (tool calling, parallel-call control, context window) and one completion operation.
3. **Retry, then fall back.**
   - The primary is retried on retryable errors with backoff.
   - Then the configured secondary is tried, **only if** it is capability-qualified and the rendered history fits its context window with a safety margin.
   - History is never truncated or summarised to make it fit. Otherwise the result is a typed failure (fallback unrepresentable, or provider unavailable), and the run fails.
   - **Each turn starts with the primary again**; fallback is not sticky.
4. **Not provider failures.** Schema-invalid calls, unknown tools, and denials are typed results within the repair/denial budget. They never trigger fallback.
5. **Every attempt is recorded** with usage and cost per provider, including failures and calls repeated after a crash.
6. **Choosing a provider is configuration only.** The deterministic scripted provider is test-only, sits behind the same interface, and refuses to load outside the test environment.

## Alternatives Considered

### Store provider-native transcripts
- **Pros**: No translation layer.
- **Cons**: Fallback becomes a lossy conversion at the worst possible moment.
- **Why not**: Unsafe for mid-run fallback (T-10).

### Use a third-party framework's message types as the stored format
- **Pros**: Less code.
- **Cons**: Ties the persisted format to that library's evolution and to provider-specific fields.
- **Why not**: History is long-lived, checkpointed state and must be under Tether's control.

### Sticky fallback
- **Pros**: Fewer failed primary attempts during an outage.
- **Cons**: Hides primary recovery and skews cost.
- **Why not**: Per-turn retry is simpler and more honest.

### Truncate or summarise to fit
- **Pros**: Fallback "succeeds" more often.
- **Cons**: Silent loss of state.
- **Why not**: Forbidden by the PRD.

### Treat invalid output as a provider failure
- **Pros**: Uniform error handling.
- **Cons**: A weak or adversarially steered model could force failover and spin the run.
- **Why not**: Contradicts the PRD's model-output rules.

## Rationale

A neutral, Tether-owned representation makes fallback a rendering problem rather than a conversion problem. Capability and fit checks make failure explicit instead of lossy.

## Consequences

### Positive
- T-10 and T-11 hold; switching provider is configuration only.
- Cost and retry history are truthful.

### Negative
- An adapter's translation logic must be maintained for each provider.
- Capability declarations must be accurate.
- Token estimates are approximate, hence the safety margin.
- Retrying the primary on each turn adds latency during a long primary outage.

## Failure and Recovery

- If both providers fail, the run fails with an audited "provider unavailable".
- If history doesn't fit the secondary, a typed failure is returned and nothing is truncated.
- A crash during a model call re-calls the provider, and both calls are recorded.

## Invariants and Constraints

- §22 invariants 11, 14, 19, 20.

## Revisit When

- A third provider, streaming, or explicit and audited history summarisation is added.
- The PRD's open questions on the hosted provider and primary/fallback defaults are answered (configuration only, no ADR change expected).

## References

- PRD: Provider fallback; Model-output handling; Core Capabilities (provider abstraction); Decisions Log (Provider-neutral history, Fallbacks, Deterministic tests); failure path F2; tests T-10, T-11, T-22.
- Architecture: §8 AD-06, §13.3, §26, §24 item 6.
