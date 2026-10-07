# ADR-0008: Deterministic idempotency keys and stop-gated re-sends of in-flight writes

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-09; §12.2, §12.4, §15; §24 items 9, 25 (A-06); review findings D-06, D-11
**Resolves**: A-06

## Context

Replays, retries, and redeliveries of one logical execution must present the same idempotency key. Legitimately new executions must get a new one.

Open question A-06: after a stop (cancel or deadline), may an idempotent write that is already `dispatched`, and now needs a re-send, be re-sent with the same key? The alternative is to go straight to `indeterminate`. The constraints are:
- PRD: stop never hides unknown outcomes; non-idempotent writes are never redelivered; unknown after exhausted attempts becomes `indeterminate`; after cancellation the agent takes no new actions.
- T-26: expects a write in flight at cancellation to become `indeterminate`, so the answer must be deterministic.

## Decision

1. **Key derivation.** The key is derived deterministically from `(run_id, step_ordinal, call_hash)`, using an unambiguous canonical encoding and a cryptographic hash, with a recognisable prefix. The formula is in architecture AD-09. It is computed **once, when the ledger row is created, and stored**. Every send uses the stored key; it is never recomputed. Because the call hash is keyed (ADR-0010), the idempotency key reveals nothing about the arguments.
2. **One key per logical execution.** The first dispatch, retries within a delivery, and redeliveries after lease expiry all use that key and share one attempt counter. A re-proposal after `not_applied`, rejection, or skip comes from a new model turn, so it gets a new ordinal and a new key.
3. **Non-idempotent writes are sent at most once.** Any unknown outcome after `dispatched` becomes `indeterminate`, and the write is never re-sent.
4. **Before every send**, the worker checks two things: the payload arguments hash to the ledger's call hash, and the pinned tool version resolves (ADR-0011). If either check fails before the first send, the entry goes `pending → failed` (not applied). If it fails before a re-send, the outcome is unknown and rule 5 applies.
5. **A-06: every external send is gated by the stop predicate.**
   - The first send is gated by TX6, as today.
   - Before each re-send, whether a retry or a redelivery, the worker first commits a **re-dispatch record**. That record requires the entry to still be `dispatched` and the stop predicate to be false (checked as in TX6). It increments attempts and appends an observation audit event. Only after it commits does the worker send.
   - If the stop predicate is true, **nothing is re-sent**. In that same transaction a write goes `dispatched → indeterminate` (a read resolves to a typed failure), with audit and a wake.
6. An attempt **already in flight** when the stop commits is not interrupted, because a dispatched write cannot be recalled. If it returns a definitive result, that result is recorded. If its outcome is unknown, the entry becomes `indeterminate` immediately, whatever attempts remain.
7. **Without a stop**, recovery re-sends with the same key while attempts remain. F4, F9 and C-04 to C-07 behave as specified today.

## Alternatives Considered

### Re-send after a stop with the same key
- **Pros**: Often resolves the unknown automatically (the target returns the original result), so fewer reconciliations.
- **Cons**: If the first attempt never reached the target, the re-send *performs* the write after the requester or deadline stopped the run, which is a new effect on the world after the stop. T-26's outcome would also depend on target timing.
- **Why not**: It conflicts with "after cancellation, the agent takes no new actions" and makes T-26 nondeterministic.

### Gate only redeliveries, or only in-delivery retries
- **Pros**: Fewer extra transactions.
- **Cons**: Two rules for the same risk; the outcome would depend on why the re-send was needed.
- **Why not**: Inconsistent and harder to test.

### Abort the in-flight attempt when a stop is requested
- **Pros**: Faster stop.
- **Cons**: Aborting mid-request turns a possibly known outcome into an unknown one, and recalls nothing.
- **Why not**: No benefit, and less information.

### Random keys stored only in the checkpoint; keys from the call hash alone; keys from the model's `call_id`
- **Pros**: Simpler derivation.
- **Cons**: Random checkpoint keys are lost on a crash before the checkpoint. Hash-only keys collide when the model legitimately repeats an identical action later in the run. Model call IDs are neither stable across replays nor across providers.
- **Why not**: None of them is both stable for one logical execution and unique across executions.

## Rationale

There is one rule: **no external send begins after a stop is visible.** It uses the same gate pattern as TX6 and as provider attempts (ADR-0006), keeps the PRD's no-new-actions semantics, never hides an unknown outcome, and makes T-26 deterministic. Idempotent recovery without a stop is unchanged.

## Consequences

### Positive
- T-26 is deterministic, and "stop" has a crisp meaning for every send path.
- Stored keys mean retries, redeliveries, and replays can never present different keys.

### Negative
- Cancelling during a transient blip on an idempotent write creates reconciliation work that a re-send would have avoided.
- One small extra transaction per re-send.

### Risks
- A target that retains idempotency keys for less time than recovery takes could re-apply a write on redelivery. This is documented for future integrations; the demo target retains keys durably.

## Failure and Recovery

- A crash between the re-dispatch record and the send means the attempt is counted. The next delivery reuses the same key, and exhaustion leads to `indeterminate`.
- A stop during an outage: no re-send happens, the entry becomes `indeterminate`, and the reconciler can query the target using the stored key.

## Invariants and Constraints

- §22 invariants 1, 4, 5, 6, 9.
- New invariant **35** (added to architecture §22). No external send, first or repeat, starts unless a committed gate transaction observed the stop predicate as false; after a stop, a dispatched write with an unknown outcome becomes `indeterminate` without a re-send.

## Revisit When

- Integrations offer status lookup by idempotency key, so Tether could query instead of re-sending after a stop.
- Post-execution confirmation is added.
- Integrations with non-durable key retention are onboarded.

## References

- PRD: Dispatch and side-effect outcome semantics; Cancellation; Run lifecycle; Decisions Log (Queue delivery, Durability claim, Ambiguous writes); failure paths F4, F8, F9, F11; tests T-20, T-26, C-04–C-07.
- Architecture: §12.2, §12.4, AD-09, §13.1 (TX6, TX7, TX12), §15, §24 items 9, 25, §27 A-06, D-06, D-11.
