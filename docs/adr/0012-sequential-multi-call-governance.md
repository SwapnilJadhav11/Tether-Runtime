# ADR-0012: Sequential governance of multi-call model turns with typed `skipped` remainder

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: §8 AD-06 (capabilities), §9.1–9.2, §21 Q3; §24 items 16, 22 (A-03)
**Resolves**: A-03

## Context

A model turn can contain several tool calls. Some providers, including local models, cannot disable this. The PRD requires every call to be validated, policy-checked, approved, and ledgered on its own, with no batch approvals. Providers also require a result for every call ID before the next turn. And the model usually plans later calls on the assumption that earlier ones succeed: "update config, then restart". Open question A-03 asks what happens to calls k+1…n when call k does not succeed.

## Decision

1. **Sequential governance.**
   - Adapters disable parallel tool calls where the provider allows.
   - Any multi-call response is processed **in returned order**. Each call has its own stable step ordinal and is validated, evaluated, approved, and ledgered independently. There is never a batch approval.
   - Calls k+1…n are not validated, policy-evaluated, or sent for approval until call k reaches a final result.
2. **Continue.** If call k's final result is **`applied`** (automatic, or reconciled as applied), governance moves on to call k+1.
3. **Pause, not skip.** These states hold the whole sequence while the remaining calls wait ungoverned:
   - `require_approval` on call k;
   - call k in flight (awaiting tool);
   - call k `indeterminate` (awaiting reconciliation).
   When call k resolves, rule 2 or rule 4 applies to its final result. Remaining calls are then governed against the policy current at that time.
4. **Skip.** Any other final result for call k makes each remaining call receive a typed **`skipped`** result, which references call k's call ID and outcome. Such results are:
   - `invalid` (schema-invalid or unknown tool);
   - `denied` (at proposal or at TX5 re-evaluation);
   - `rejected`;
   - `expired` or lapsed approval;
   - `not_applied` (including failed precondition, tool version unavailable, cancelled before dispatch);
   - a typed read failure;
   - reconciled `not_applied`.
   Skipped calls get no validation, policy evaluation, approval, or ledger row. Each skip is recorded as a decision-class audit event for its ordinal (create-or-get, fenced per ADR-0005).
5. **What the model receives.** Exactly one result per call ID, in original order. `skipped` is neither a failure nor a denial. It consumes no repair/denial budget and is not an execution. A re-proposal in the next turn gets a new ordinal, a new key, and fresh policy and approval.
6. **Terminating results.** If the stop predicate is true, a reconciliation abandons, a read tool is configured to fail the run, a budget is exhausted, or providers are unavailable, the run ends. Remaining calls are neither governed nor marked skipped, and no further model call happens.

## Alternatives Considered

### Continue with the remaining calls regardless
- **Pros**: Fewer model turns.
- **Cons**: Executes later steps on a false premise, for example a restart after a denied config change.
- **Why not**: Acts on plans the model would likely change.

### Govern all calls up front / batch approvals
- **Pros**: One approval interaction.
- **Cons**: The PRD forbids batch approval; approvals would be requested for calls that may never become valid.
- **Why not**: Contradicts the PRD.

### Silently drop the remaining calls
- **Pros**: Simple.
- **Cons**: Providers reject transcripts with missing results, and the model never learns why.
- **Why not**: Breaks provider contracts and transparency.

### Fail the run
- **Pros**: Conservative.
- **Cons**: Turns a normal outcome (denial, rejection) into a run failure.
- **Why not**: The PRD expects the model to reason about typed results and re-plan.

### Skip only after write failures; treat `require_approval` as skip
- **Pros**: Fewer extra turns for independent reads.
- **Cons**: More rules to test. Treating approval as a skip would force a re-proposal after every approval, so multi-step approved plans could never chain.
- **Why not**: A single uniform rule is simpler and still allows chaining.

## Rationale

The remaining calls were planned on the premise that earlier calls succeed. The sequence continues exactly when that premise holds, and otherwise hands control back to the model with full, typed information. Staleness during pauses is already covered for each call by policy re-evaluation, approval, and preconditions.

## Consequences

### Positive
- Tether never acts on a falsified premise.
- Transcripts are valid for every provider.
- Behaviour is deterministic and easy to script (the multi-call scenario in §21 Q3).

### Negative
- Any non-applied result costs an extra model turn, even when the remaining calls were independent.
- During long pauses the remaining calls stay pending, and are governed later under the then-current policy.

## Failure and Recovery

- After a crash mid-sequence, the ordinals and remaining calls are still in the checkpoint. Skips follow deterministically from the persisted result of call k, and skip events are create-or-get.

## Invariants and Constraints

- §22 invariants 3, 11, 15, 22, 27.
- New invariant **32** (added to architecture §22). Calls after call k are governed only if call k's final result is `applied`; otherwise each gets a typed `skipped` result; `require_approval` pauses and never skips.
- The neutral history's tool-result outcomes gain `skipped` (AD-06).

## Revisit When

- Providers reliably support controlled parallel calls, and tools can declare independence.
- Batch-approval UX is requested (that would require a PRD revision).

## References

- PRD: Model-output handling; Core Capabilities (approval gate: each call bound and approved individually); Provider fallback (neutral history); failure paths F5, F6, F10; tests T-05, T-06, T-15, T-21, T-22.
- Architecture: §8 AD-06, §9.1, §9.2, §21 Q3, §24 items 16, 22, §27 A-03.
