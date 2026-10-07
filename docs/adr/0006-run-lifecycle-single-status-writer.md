# ADR-0006: Run lifecycle: single status writer, stop predicate, terminal immutability

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-07; §9.2 stop predicate; §11.4; §15; §24 item 7; review findings F-06, D-02, D-03

## Context

Cancellation, deadline expiry, reconciliation, and graph progress all race with each other. The PRD requires:
- one lifecycle definition;
- no recall of dispatched writes;
- unknown outcomes never hidden by a cancellation or timeout;
- cancelled or timed-out runs never resume model execution, even after reconciliation;
- terminal states never change.

Exact status names are left to the architecture.

## Decision

1. **Only the run-driver module writes `runs.status`**, either inside a graph invocation or through the TX12 `advance_run` dead-letter function. The one exception is the initial `queued` insert at submission. The API and timers write *requests* (cancel-requested by/at, a deadline-exceeded marker, approval and reconciliation rows), then insert a wake job.
2. Every transition is a **conditional update on the expected status**, fenced per ADR-0005. A database-level guard rejects any change to status once it is terminal. Other columns are unaffected, and cancel or deadline requests against a terminal run are no-ops.
3. **Stop predicate:** cancel requested, OR database clock ≥ the run's stored deadline (fixed at submission). It is evaluated at these points:
   - graph entry and loop;
   - before every provider attempt, including retries and fallback;
   - immediately after a model response returns (proposals received after a stop are recorded for cost and never governed);
   - at the top of each write node, before a ledger row exists;
   - before every external send of a tool call (ADR-0008).
4. **Serialisation.**
   - Cancel (TX9) and deadline (TX10) lock the run row `FOR UPDATE` and close pending approvals atomically.
   - TX2, TX3 and TX5 take `FOR SHARE` and refuse when the stop predicate is true.
   - Approval decisions (TX4) and reconciliation (TX8) take `FOR UPDATE`.
   - Linearisation point: a model request started before the stop commit became visible is not a "further" call.
5. **Precedence.**
   - A stop with a non-terminal ledger entry means the run waits (shown as awaiting tool plus the stop flag).
   - A stop with an `indeterminate` entry means the run shows as awaiting reconciliation.
   - A run is never cleanly `cancelled` or `timed_out` while an outcome is unknown.
   - There is no separate "stopping" status.
   - Reconciling a stopped run only records the outcome and moves the run to a terminal state; there is no further model call or tool proposal.
   - Terminal records carry an outcome summary, so a reconciled cancellation is never reported as clean.
6. The architecture's status set is the working set: queued, running, awaiting approval / tool / reconciliation, succeeded, failed, cancelled, timed out, abandoned. Planning may rename statuses, but the transition rules above are fixed.

## Alternatives Considered

### API endpoints write terminal statuses directly
- **Pros**: Cancellation is immediately visible in status.
- **Cons**: Lost-update races with an in-flight graph; "never resume after cancel" would depend on timing.
- **Why not**: It cannot be made deterministic for T-25/T-26.

### An explicit "stopping" status
- **Pros**: Visible intermediate state.
- **Cons**: Extra transitions, and it carries no information the stop flag doesn't already carry.
- **Why not**: Removed as a simplification in review (S-*).

### Enforce the deadline only by timer job
- **Pros**: One mechanism.
- **Cons**: Late whenever workers are down.
- **Why not**: Decision-time enforcement against the stored deadline is required (invariant 16).

### Rely only on application-level checks for terminal immutability
- **Pros**: No trigger.
- **Cons**: A bug could overwrite a terminal state.
- **Why not**: Immutability is a PRD guarantee and is worth a database guard.

## Rationale

With one writer and request flags, there is exactly one place to reason about transitions. Cancellation, deadline, and reconciliation compose deterministically. Every gate that matters re-reads one shared predicate under well-defined locks.

## Consequences

### Positive
- T-25 and T-26 are deterministic.
- Unknown outcomes can never be hidden by a clean status.
- A stop takes effect at every gate even before the driver runs.

### Negative
- Status updates are asynchronous: a cancel HTTP call returns once the flag commits, and status changes only when the driver runs (later if workers are down). Clients must read the flag alongside the status.
- Status names are not yet frozen for API consumers.

## Failure and Recovery

- A crash between deciding a transition and writing it is replayed; conditional updates make transitions idempotent.
- A dead `advance_run` job resolves through TX12 (see ADR-0004).

## Invariants and Constraints

- §22 invariants 8, 9, 16, 22, 28, 29.

## Revisit When

- Inline human override or pause/resume (a PRD Should-have) is designed.
- Service-principal runs are introduced.
- Run driving moves out of the worker.

## References

- PRD: Run lifecycle; Cancellation; Reconciliation ("cancelled or timed-out runs never resume"); Core Capabilities (single lifecycle definition); Decisions Log (Run lifecycle, Cancellation, Reconciliation after cancel/timeout); failure path F11; tests T-25, T-26.
- Architecture: §9.2 (stop predicate), §10 AD-07, §11.4, §13.1 (TX9–TX11), §15, §21 Q8 and Q10, §27 F-06, D-02, D-03.
