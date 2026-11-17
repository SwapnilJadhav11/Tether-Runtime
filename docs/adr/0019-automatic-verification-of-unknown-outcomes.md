# ADR-0019: Execution ledger, honest outcome classification, bounded automatic verification of unknown outcomes, and human reconciliation as the fallback

**Date**: 2026-11-09
**Status**: accepted (2026-11-09; supersedes [ADR-0007](0007-execution-ledger-honest-outcomes.md))
**Deciders**: Tether project owner (SwapnilJadhav11); drafted during the 2026-11 documentation reconciliation, accepted by the owner on 2026-11-09 with the required semantics listed under "Acceptance record"
**Architecture source**: §12.1–12.4; §15; §13.1 (TX7, TX8, TX12); product direction [TDR-007](../product/tether-v1-direction-2026-11.md), [U-009](../product/tether-v1-direction-2026-11.md); scope delta [C-1](../product/tether-v1-scope-delta.md)

> **Why a complete restatement.** The ADR lifecycle in this repository has no partial-supersession state. ADR-0007's rules 1–6 are carried over unchanged; rules 7 and 8 are replaced by rules 7–12 below. Nothing in this ADR weakens the conservative classification or the prohibition on blind re-sends.

## Context

ADR-0007 set the honest guarantee boundary: no duplicate supported writes, no automatic redelivery of a non-idempotent write after dispatch, no "not applied" report without positive evidence, and `indeterminate` for every unknown outcome. It also decided that **only a human** resolves `indeterminate`, and rejected "auto-resolve on a late result, or by probing the target" because probing needs per-integration read-back semantics and post-execution confirmation was deferred by the PRD.

The accepted product direction (TDR-007) changes the second half of that position. Real integrations (the GitHub sandbox selected as the V1 external API) offer safe, idempotent read-backs ("does issue N exist with label L?"). Requiring a person to answer a question the target can answer definitively turns a frequent, recoverable condition into human work and slows every unattended scenario. The direction requires:

- a tool may declare a safe read-back/verification capability;
- when a consequential action's outcome is unknown, Tether tries that capability before requiring a person;
- verification must not re-send the original write, must be separately authorized as appropriate, bounded, attributable and auditable;
- human reconciliation remains the safe fallback.

Constraints that must survive: invariants 1, 4, 5, 7, 9, 22 and 35 (architecture §22); ADR-0008 (no external send after a stop without a committed gate); ADR-0006 (single status writer, stop precedence); ADR-0018 (a run awaiting reconciliation stays held until its entries are resolved); ADR-0017 (masking before persistence); ADR-0015 (critical events inside the state-changing transaction).

## Decision

### Carried over from ADR-0007 (unchanged)

1. **One ledger row per logical execution**, unique per `(run, step_ordinal)`, created only by TX5. The row carries the keyed call hash, pinned tool version, effect, idempotency support and idempotency key. Reads use the ledger too; reads never become `indeterminate`.
2. `pending → dispatched` commits, with a critical audit event, **before any external request**.
3. **Every side-effecting result is `applied`, `not_applied` or `unknown`.** `not_applied` requires positive evidence (a definitive rejection declared by the tool through the SDK outcome type, a failure provably before any bytes left Tether, or a refusal before dispatch). Everything else after dispatch is `unknown`.
4. An `unknown` result is re-sent only under ADR-0008: same key, idempotent tool, attempts left, no stop. Otherwise it becomes **`indeterminate`**, including for idempotent tools whose attempts are exhausted.
5. **`indeterminate` is a first-class ledger state and run condition**, not a failure. The model is never told the write failed. The run does not continue automatically on the strength of the write alone.
6. Outcome commits are conditional only on the entry still being `dispatched`: the first definitive outcome wins. They do not depend on the lease token and always enqueue a wake.

### New: bounded automatic verification

7. **Verification is a tool-declared capability, not a re-send.**
   - A write tool may declare a `verify` operation in its descriptor. It takes the original validated arguments and the stored idempotency key, performs only **safe reads** against the target, and returns exactly one of: `confirmed_applied(evidence)`, `confirmed_not_applied(evidence)` or `inconclusive(evidence)`.
   - A verify operation **must never** re-issue the write, mutate the target, or send the original request bytes. The SDK documents this contract; the framework treats any exception from `verify` as `inconclusive`.
   - Tools without a declared `verify` operation are **not** verified; their `indeterminate` entries go straight to human reconciliation (rule 11).
   - Invariant 4 (a non-idempotent dispatched write is never re-sent) and invariant 35 (no external send without a stop-gated gate) are unchanged. A verification probe is an external *read*, not a send of the write, but it is gated the same way (rule 9).
8. **Authorization and policy.**
   - Verification is attempted only for a ledger entry that reached `indeterminate` through TX7, TX6r or TX12, i.e. only after a TX5-authorized, TX6-dispatched write. No new policy decision on the *write* is made.
   - Before each probe, the framework evaluates current policy for the **verify operation** with the same trusted inputs as TX5 (requester snapshot, agent, workspace, tool descriptor from the registry, policy version read in the same transaction per ADR-0009). The decision set is `allow` or `deny` only; `require_approval` is not meaningful for a read-back and is treated as `deny`. A `deny` ends automatic verification and hands over to human reconciliation.
   - The pinned tool version must still resolve (ADR-0011). If it does not, verification is skipped and reconciliation is required.
9. **Bounded attempts and stop behaviour.**
   - Verification runs as its own job kind (`verify_outcome`) enqueued by the transaction that moved the entry to `indeterminate`, so it never blocks TX7/TX6r/TX12 and survives worker restarts.
   - Each probe is preceded by a committed **verification gate** (TX-V1) that requires: entry still `indeterminate` with `resolution IS NULL`, verification attempts below the descriptor's bound, the stop predicate observed **false**, and a policy `allow` recorded in the same transaction. The gate increments `verification_attempts` and appends the observation audit event `VERIFICATION_ATTEMPTED`. Only after it commits does the probe run.
   - The bound is per descriptor (`verify_max_attempts`, default 3) with a per-descriptor delay between probes (`verify_backoff`, default base 1 s, ×2, cap 30 s, full jitter). Exhaustion without a definitive result ends automatic verification.
   - **Stop precedence.** If the stop predicate is true (cancel requested or deadline passed), no probe starts: the gate records `VERIFICATION_SKIPPED` with reason `stopped` and the entry goes to human reconciliation. A probe already in flight when a stop commits is not interrupted; its result is still recorded under rule 10, because a read-back performs no new action on the world. After a stop, a verified `applied` or `not_applied` **still only terminates the run** (ADR-0006 rule 5): the run moves to `cancelled` or `timed_out` with the verified outcome in its summary and no further model call.
10. **Supported verification outcomes and evidence persistence.**
    - `confirmed_applied` → TX-V2: conditional `indeterminate → completed`, `resolution = verified_applied`, `resolved_by = system:verification`, masked evidence stored on the entry, critical audit event `VERIFIED` (decision class, keyed by ordinal), wake enqueued. The model receives a typed `applied` result whose payload is the **verification evidence**, masked, marked `verified`; the original output of the write is not known and is not fabricated.
    - `confirmed_not_applied` → TX-V2: conditional `indeterminate → failed`, `resolution = verified_not_applied`, same audit and wake. The model receives a typed `not_applied` result. Any re-proposal is a new logical execution with a new ordinal and key and passes through policy and approval again, exactly as after a reconciled `not_applied`.
    - `inconclusive` → recorded as the observation event `VERIFICATION_INCONCLUSIVE` with masked evidence; the entry stays `indeterminate`; the next probe, if any, is scheduled.
    - Evidence passes through the ADR-0017 sanitizer before persistence, like every tool output. Unmasked evidence is never persisted, logged, traced or sent to a model.
    - A probe result arriving after the entry was resolved by a human (TX8 committed first) is recorded as `VERIFICATION_LATE` evidence and changes nothing: the conditional update finds `resolution IS NOT NULL` and is a no-op. The human decision stands; the late result is visible to auditors.
11. **Transition to human reconciliation.** Human reconciliation (TX8) is required when any of these holds: the tool declares no `verify`; policy denied the probe; the pinned version is unavailable; the stop predicate was true; attempts are exhausted without a definitive result. In each case the transaction that ends automatic verification appends `VERIFICATION_EXHAUSTED` (with its reason) and the run shows `awaiting_reconciliation`. Human reconciliation itself is unchanged from ADR-0007 rule 8: `tether-ops`, `reconciler` role, policy eligibility over reconciler and requester, `applied` / `not_applied` / `abandon` with justification, critical audit event `RECONCILED`, resolution recorded as `reconciled_*`. A human may also reconcile **while** automatic verification is still in progress; TX8 takes the run row `FOR UPDATE` and wins by committing first, after which rule 10's late-result handling applies.
12. **Effects on the run lifecycle and ADR-0018.**
    - The run condition for an `indeterminate` entry remains `awaiting_reconciliation`; no new run status is introduced. While verification is pending the inspection API shows the entry as `indeterminate` with `verification: in_progress` and the attempt count.
    - ADR-0018 is preserved: a run in `awaiting_reconciliation` stays held while any ledger entry is unresolved; an entry is now resolved by reconciliation (TX8), by its own recorded outcome (TX7/TX12), **or by verification (TX-V2)**. Each of these inserts its own wake (ADR-0004).
    - `resolution` gains the values `verified_applied` and `verified_not_applied`, distinct from `automatic` and `reconciled_*`, so audit and inspection always show whether a person or a probe resolved the entry.
    - The model-visible outcomes stay `applied` and `not_applied`; a verification marker is included in the payload so the model knows the result was inferred by read-back.

## Alternatives Considered

### Keep ADR-0007 (human-only resolution)
- **Pros**: Simplest honest contract; nothing new is exposed to the target.
- **Cons**: Every transient ambiguity becomes human work; unattended scenarios and the GitHub proof cannot complete without an operator.
- **Why not**: The accepted direction (TDR-007) requires verification first; the conservative classification is kept.

### Re-send with the same key after a stop or exhaustion instead of probing
- **Pros**: No new SDK surface.
- **Cons**: Performs the write after a stop if the first attempt never arrived (rejected in ADR-0008); impossible for non-idempotent writes.
- **Why not**: Violates invariants 4, 9 and 35.

### Treat a late definitive result as auto-resolving
- **Pros**: No probe needed when the target eventually answers.
- **Cons**: Late results can be partial or stale, and they arrive outside any gate.
- **Why not**: ADR-0007's reasoning stands; a late result is recorded as evidence only.

### Let the model call a read tool to "check" and decide for itself
- **Pros**: No framework change.
- **Cons**: The model would be told, in effect, that the write failed or needs checking, which is the false-failure channel ADR-0007 closes; the model is not a trusted resolver.
- **Why not**: Resolution must be a framework decision with evidence, not a model inference.

### Verify synchronously inside TX7
- **Pros**: Fewer jobs.
- **Cons**: Holds a transaction across an external call and blocks the outcome commit; a crash mid-probe would lose the attempt count.
- **Why not**: Separate gated job keeps the TX7 contract and crash safety.

## Rationale

The target is often the only party that knows what happened. Asking it a safe question, under the same authorization, stop and masking discipline as every other external interaction, resolves most ambiguities without a person and without ever repeating the action. Everything that could make this unsafe is excluded by construction: no re-send, no new write, no model-driven inference, bounded attempts, a committed gate before every probe, and a human who can always step in and always wins on conflict.

## Consequences

### Positive
- Most `indeterminate` outcomes against targets with read-backs resolve in seconds with persisted evidence.
- Human reconciliation remains available, unchanged and authoritative.
- Audit distinguishes automatic, verified and reconciled resolutions.

### Negative
- New SDK surface (`verify`, `verify_max_attempts`, `verify_backoff`) and a new job kind with its own gate transaction.
- A mis-implemented `verify` that mutates the target would break the guarantee. Mitigation: SDK contract, review, and a contract test that asserts no write calls during verification against the demo target and the GitHub sandbox.
- Evidence payloads add to masked storage.

### Risks
- A verify operation reports `confirmed_not_applied` for a write the target did apply (false negative) and the model re-proposes it, causing a duplicate. Mitigation: `confirmed_not_applied` must be backed by a definitive absence check keyed by the idempotency key or an equivalent unique marker; tool authors are told to return `inconclusive` otherwise.

## Failure and Recovery

- Worker crash during a probe: the `verify_outcome` job is redelivered; the gate finds the attempt already counted and either schedules the next probe or exhausts to reconciliation.
- Target unavailable: probes return `inconclusive`; exhaustion leads to reconciliation.
- Stop during verification: no further probe; the in-flight probe's result, if definitive, is recorded and the run terminates with that outcome in its summary.
- Database down: nothing progresses, nothing is lost.

## Invariants and Constraints

- §22 invariants 1, 4, 5, 7, 9, 22, 35 unchanged. Cancellation, fencing, idempotency and stop semantics are not weakened: TX-V1 and TX-V2 are worker transactions in the same class as TX6/TX7 (ADR-0005 rule 7), serialised by the ledger's conditional updates rather than by the driver generation; the run driver's reaction to a `verified_*` resolution is an ordinary fenced driver write; no probe starts without a committed gate that observed the stop predicate false; a probe never sends the write and never consumes the write's send budget.
- New invariant **36** (to be added to architecture §22 on acceptance): an `indeterminate` entry is resolved only by a committed TX8 (human), or by a committed TX-V2 whose probe was preceded by a committed gate that observed the stop predicate false and a policy `allow`; a verification probe never sends the original write or any mutation.
- New invariant **37**: `resolution` records whether an outcome was automatic, verified or reconciled, and model-visible payloads carry the verification marker.

## Acceptance record (2026-11-09)

The owner accepted this ADR with the following required semantics, each mapped to the rule that carries it: verification is optional and explicitly declared by the tool (rule 7); the read-back is itself non-side-effecting (rule 7); a definitive `applied` may resolve the entry automatically (rule 10); a definitive `not_applied` may resolve it under the normal run semantics, so a re-proposal is a new logical execution (rule 10); inconclusive results, verifier errors and unavailable verification leave the entry `indeterminate` (rules 7, 10, 11); human reconciliation remains the final fallback (rule 11); every attempt and result is attributable and auditable (rules 9, 10); cancellation, fencing, idempotency and stop semantics are not weakened (rule 9 and Invariants). No rule needed a change of meaning; the fencing-class sentence under Invariants was added at acceptance as a clarification.

## Documents and tests changed at acceptance, and still to change

- Changed with acceptance: `docs/adr/README.md` (ADR-0007 → `superseded by ADR-0019`; this ADR → `accepted`); architecture markers (`[ADR-0019 proposed]` → required design, not yet implemented); PRD, scope delta, guarantee card, plan, CLAUDE.md, README wording.
- Still to change when W5.1 implements it:
- Architecture: §7 (descriptor `verify`), §12.2 state diagram (`indeterminate → completed/failed` via verification), §12.4, §13.1 (TX-V1, TX-V2, `verify_outcome` job), §15 sequence diagram, §22 invariants 36–37, §18 (`resolution` values, `verification_attempts`).
- PRD: reconciliation section already revised to "verification first, human fallback" conditional on this ADR.
- SDK: `ToolDescriptor.verify`, `verify_max_attempts`, `verify_backoff`; `VerificationResult` type.
- Schema: `ledger_entries.verification_attempts`, `verification_evidence_masked`; `resolution` CHECK extended; new job kind.
- Tests: gate matrix (stop, deny, version gone, exhaustion), evidence masking, late-probe no-op, human-wins race, no-mutation contract test against the demo target and the GitHub sandbox, T-12 reconstruction with `VERIFIED` events.

## Defaults adopted at acceptance (U-009; the owner may revise any of them before W5.1 implements it)

1. A verification `deny` from policy is an observation event; it is not a model action and does not count toward the repair/denial bound (D-11).
2. `confirmed_applied` is surfaced to the model as `applied` with the masked evidence and a verification marker, not as a new neutral-history outcome.
3. `verify_max_attempts` defaults to 3; `verify_backoff` is base 1 s, ×2, cap 30 s, full jitter.
4. `verify` may be declared only on write tools; reads never become `indeterminate`.

## Revisit When

- Integrations offer status lookup by idempotency key for every write, making verification the common path.
- Automated reconciliation *policies* (resolving without a definitive read-back) are requested; that would need a new ADR, not an extension of this one.

## References

- PRD (revision 2026-11-09): Dispatch and side-effect outcome semantics; Outcome verification and reconciliation; tests T-17, T-20, T-25, T-27 (new), C-07.
- Architecture: §7, §12.1–12.4, §13.1, §15, §22.
- ADRs: [ADR-0004](0004-postgres-job-queue.md), [ADR-0006](0006-run-lifecycle-single-status-writer.md), [ADR-0007](0007-execution-ledger-honest-outcomes.md), [ADR-0008](0008-idempotency-keys-and-stop-gated-resends.md), [ADR-0009](0009-policy-engine-current-policy.md), [ADR-0011](0011-tool-version-pinning-approval-validity.md), [ADR-0015](0015-per-run-hmac-audit-and-authoritative-reconstruction.md), [ADR-0017](0017-sensitive-data-masking-choke-point.md), [ADR-0018](0018-awaiting-reconciliation-hold.md).
- Product: [direction record](../product/tether-v1-direction-2026-11.md) TDR-007, U-009; [scope delta](../product/tether-v1-scope-delta.md) C-1, D-3.
