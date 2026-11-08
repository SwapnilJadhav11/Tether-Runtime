# ADR-0018: Clarification of ADR-0006: a run awaiting reconciliation is held until its ledger entries are resolved

**Date**: 2026-11-08
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11)
**Architecture source**: AD-07 (§10 state diagram); §11 AD-08 and §13.1 TX12; plan §3 PD-35 and §15 item 5

## Context

ADR-0004's TX12 moves a run to `awaiting_reconciliation` when its `advance_run` job exhausts its deliveries while a ledger entry is still in flight or `indeterminate`. ADR-0006 and the architecture's state diagram do not say what a later wake does with such a run. In particular, they do not say how the run leaves `awaiting_reconciliation` when the blocking entry reaches a terminal state on its own (TX7 or TX12) rather than through reconciliation (TX8). The plan records this as a gap (§15 item 5), adopts the default PD-35, and asks for a clarification note on ADR-0006. Accepted ADRs are not edited, so this ADR records the clarification instead.

## Decision

This ADR clarifies ADR-0006. It does not supersede it and changes none of its other decisions.

1. A run in `awaiting_reconciliation` stays held while any of its ledger entries is unresolved (`pending`, `dispatched` or `indeterminate`). A wake that finds it held does nothing: the driver claims no fencing token, changes no status and makes no graph or model call.
2. Once every entry is resolved, a later wake may resume the run under the normal run-driver rules (ADR-0005, ADR-0006). An entry is resolved either by reconciliation (TX8) or, for one that was still in flight, by its own recorded outcome (TX7 or TX12). Each of these transactions inserts its own wake (ADR-0004).
3. Resuming the run is the run driver's ordinary claim: a conditional, fenced status transition made under the run's advisory lock. Only the run-driver module writes `runs.status` (ADR-0006 rule 1).

## Alternatives Considered

### Alternative 1: Resume on any wake
- **Pros**: no extra check in the driver.
- **Cons**: a run whose `advance_run` keeps failing would cycle without end: exhaustion, TX12, wake, retries, exhaustion.
- **Why not**: the hold is what makes TX12 terminate.

### Alternative 2: Leave the run only through reconciliation (TX8)
- **Pros**: matches the state diagram's existing "reconciled" edges exactly.
- **Cons**: an entry that completes automatically would leave the run waiting for a human with nothing to reconcile.
- **Why not**: it strands runs that ADR-0004 says must never be stranded.

## Rationale

The hold keeps a run from moving on while a write's outcome is unknown or still pending. That keeps outcomes honest (ADR-0007), and it means a repeatedly failing driver cannot loop. Resuming through the ordinary fenced claim keeps the single status writer and the fencing guarantees of ADR-0005 and ADR-0006 unchanged.

## Consequences

### Positive
- Exhausting `advance_run` with a write in flight always ends: the run is either held for an outcome or resumes once there is one.
- No new status and no new writer.

### Negative
- Each wake on a held run reads the run's ledger entries.

### Risks
- If a resolving transaction ever failed to insert its wake, a held run could be stranded. ADR-0004 already makes that wake mandatory.

## Failure and Recovery

A wake on a held run is a no-op, so duplicate or redelivered wakes are harmless. A crash between a resolving transaction and the next wake loses nothing: the wake job is durable and is delivered later.

## Invariants and Constraints

- §22 invariants 4, 8 and 25.
- Rules from ADR-0004, ADR-0005, ADR-0006 and ADR-0007 are unchanged.

## Revisit When

- The run lifecycle gains a new waiting status or a new way to resolve a ledger entry.

## References

- PRD: Dispatch and side-effect outcome semantics; Reconciliation.
- Architecture: §10 (AD-07), §11 (AD-08), §12.2, §13.1 (TX7, TX8, TX12).
- ADRs: [ADR-0004](0004-postgres-job-queue.md), [ADR-0005](0005-run-driver-mutex-and-fencing.md), [ADR-0006](0006-run-lifecycle-single-status-writer.md), [ADR-0007](0007-execution-ledger-honest-outcomes.md).
- Plan: §3 PD-35; §15 item 5.
