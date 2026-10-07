# ADR-0007: Execution ledger, honest outcome classification, and human-only resolution of `indeterminate`

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: §12.1–12.4; §15; §24 item 17

## Context

The PRD sets an honest guarantee boundary:
- no duplicate *supported* writes;
- no automatic redelivery of non-idempotent writes after dispatch;
- no "not applied" report without positive evidence;
- unknown outcomes always become `indeterminate`, which halts automatic continuation until an eligible human reconciles.

Telling a model "failed" when a write may have happened invites a second, duplicate action. Silently retrying can duplicate a non-idempotent write.

## Decision

1. **One ledger row per logical execution**, unique per `(run, step_ordinal)`, created only by TX5. The row carries the keyed call hash, pinned tool version, effect, idempotency support, and idempotency key. Reads use the ledger too, so replay behaves the same for every tool, but reads never become `indeterminate`.
2. `pending → dispatched` commits, with a critical audit event, **before any external request**. No request is reachable without it.
3. **Every side-effecting result is `applied`, `not_applied`, or `unknown`.**
   - `not_applied` requires **positive evidence**, which is one of: a definitive rejection declared by the tool through the SDK outcome type (including a failed precondition enforced by the target); a failure provably before any request bytes left Tether; or a refusal before dispatch (stop, hash mismatch, tool version unavailable).
   - Everything else after dispatch is `unknown`: timeouts, connection resets, ambiguous 5xx, uncaught exceptions, worker death. Tool authors must opt in to `not_applied`.
4. An `unknown` result is retried only under ADR-0008: same key, idempotent tool, attempts left, no stop. Otherwise it becomes **`indeterminate`**. This includes idempotent tools whose attempts are exhausted.
5. **`indeterminate` is a first-class ledger state and run condition** (awaiting reconciliation), not a failure. The model is never told the write failed. The run does not continue automatically.
6. Outcome commits are conditional only on the entry still being `dispatched`: the first definitive outcome wins. They do not depend on the lease token, and they always enqueue a wake.
7. A **late definitive result after `indeterminate`** is recorded as evidence for the reconciler. It never auto-resolves the entry.
8. **Only a human resolves `indeterminate`:**
   - via `tether-ops`, by someone with the `reconciler` role;
   - with policy eligibility evaluated over the reconciler's and requester's identities (the reference policy forbids self-reconciliation);
   - choosing `applied`, `not_applied`, or `abandon`, with a written justification;
   - committed with a critical audit event (TX8) before the run resumes or terminates.
   The resolution is recorded separately from automatic outcomes. After `not_applied`, any re-proposal is a new logical execution (new ordinal, new key, fresh policy and approval). After a stop, reconciliation only records the outcome and terminates (ADR-0006).

## Alternatives Considered

### Report unknown as failure
- **Pros**: Simple model contract.
- **Cons**: The model may retry or take a compensating action that duplicates a write.
- **Why not**: The PRD forbids reporting failure without evidence.

### Retry blindly until a definite answer
- **Pros**: Fewer human interventions.
- **Cons**: Duplicates non-idempotent writes.
- **Why not**: Violates the guarantee boundary (T-17, C-07).

### Auto-resolve on a late result, or by probing the target
- **Pros**: Less reconciliation work.
- **Cons**: Late results can be partial or stale. Probing needs per-integration read-back semantics, and post-execution confirmation is deferred by the PRD.
- **Why not**: The PRD requires human reconciliation.

### Ledger for writes only
- **Pros**: Fewer rows.
- **Cons**: Two replay code paths.
- **Why not**: Uniform replay is simpler. Reads still never become `indeterminate`.

### New ledger state names
- **Pros**: More expressive.
- **Cons**: Drifts from the PRD vocabulary.
- **Why not**: PRD names are kept, with a separate resolution field plus an entry-level abandoned terminal.

## Rationale

Conservative defaults (unknown unless proven otherwise) plus a human decision for genuinely ambiguous outcomes is the only design that never lies to the model or the auditor and never duplicates an unsupported write.

## Consequences

### Positive
- No false failures and no duplicate non-idempotent writes.
- The audit trail states exactly what is known.
- T-17, T-20, T-21 and C-04 to C-07 are directly provable.

### Negative
- Every `indeterminate` entry is human work.
- Runs can wait indefinitely awaiting reconciliation. The deadline does not auto-resolve them, though they hold no compute.
- Tool authors who misclassify definitive rejections will see extra `indeterminate` entries.

### Risks
- A tool author wrongly declares `not_applied` (a false negative). Mitigation: SDK documentation, review of tool outcome code, and the conservative framework default.

## Failure and Recovery

- Worker death after `dispatched` is handled per ADR-0008. Job exhaustion is handled by TX12 (ADR-0004).
- If no reconciler is available, the run waits without holding compute.

## Invariants and Constraints

- §22 invariants 1, 4, 5, 7, 22.

## Revisit When

- Post-execution confirmation checkpoints or tool-declared read-back probes are added (deferred by the PRD).
- Real external integrations with limited idempotency-key retention are onboarded.
- Automated reconciliation policies are requested.

## References

- PRD: Core Capabilities (ambiguous-write handling, reconciliation API); Dispatch and side-effect outcome semantics; Reconciliation; Decisions Log (Ambiguous writes, Durability claim, Reconciliation); failure paths F4, F7, F8, F9, F10; tests T-17, T-20, T-21, T-25, C-04–C-07.
- Architecture: §7 (tool result contract), §12.1–12.4, §13.1 (TX5–TX8, TX12), §15, §21 Q9, §26.
