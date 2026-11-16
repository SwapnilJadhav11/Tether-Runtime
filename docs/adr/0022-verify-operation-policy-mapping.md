# ADR-0022: How the verify operation is authorized: a separate, read-purpose policy decision bounded by the original action

**Date**: 2026-11-16
**Status**: accepted. Accepted by owner decision of 2026-11-16 (the Decision section quotes the owner's approved direction verbatim). The surrounding wording was drafted by an autonomous session and is pending the owner's review.
**Deciders**: Tether project owner (SwapnilJadhav11)
**Architecture source**: §12.2.1, §13.1 (TX-V1, TX-V2); invariants 11, 36, 37; [ADR-0019](0019-automatic-verification-of-unknown-outcomes.md) rules 7–12; [ADR-0020](0020-v1-policy-engine-contract.md); [ADR-0021](0021-v1-tool-contract.md) rules 14, 17, 18; plan W5.1

## Context

ADR-0019 rule 8 says that before each verification probe the framework "evaluates current policy for the **verify operation** with the same trusted inputs as TX5", that the decision set is `allow` or `deny` only, and that `require_approval` "is treated as `deny`". ADR-0021 rule 18 recorded the resulting blocker: the built-in evaluator (ADR-0020) has one rule per tool, so if the verify operation were decided by the write tool's own rule, every approval-gated write would answer `require_approval` and could never be verified. Plan W5.1 was gated on the owner deciding how the verify operation maps to policy.

The owner decided on 2026-11-16. Accepted ADRs are not edited; this ADR records the decision, turns it into enforceable rules, and states how it relates to ADR-0019 rule 8, ADR-0020 rule 1 and ADR-0021 rule 18.

## Decision

The owner's decision, verbatim (2026-11-16):

> Automatic verification does NOT require a second human approval when ALL of the following are true:
>
> 1. the verification operation is read-only / non-side-effecting;
> 2. it is predeclared in the exact approved tool version's VerifyDeclaration;
> 3. it is scoped only to verifying the exact original action / target / receipt;
> 4. it runs under the same requester, agent, workspace and policy context as the original governed action;
> 5. it is independently policy-authorized for a verification/read purpose;
> 6. it cannot gain permissions the original effective context did not have;
> 7. it cannot perform a new consequential action.
>
> If any of those conditions fail, or verification itself is ambiguous, denied, times out, or cannot establish truth: do not infer success; keep the outcome honest; classify as unknown where appropriate; enter the existing reconciliation path.
>
> Verification must never: mutate external state; broaden target scope; reuse the approval as blanket authority; bypass policy; auto-resolve ambiguity without evidence.

**In one sentence:** the verify operation is its own read-purpose action, decided by a dedicated `PolicyEngine.evaluate_verification` under the current policy version, which may only answer `allow` or `deny`, never consults the write's approval, and allows a probe only inside the exact original action's context and permissions.

### Policy decision point

1. **A fourth decision point.** `PolicyEngine` gains `evaluate_verification(VerificationRequest) → PolicyDecision`, pure and synchronous like the other three. This **amends ADR-0020 rule 1** ("exactly three methods") and the "three decision points" of ADR-0009 rule 1: the protocol now has four. Nothing else in ADR-0009 or ADR-0020 changes; reading and comparing the active `policy_version` stays in the caller's transaction (ADR-0009 rules 5–6), and every input is a trusted runtime value (ADR-0020 rule 6).
2. **Decision set.** `evaluate_verification` returns `allow` or `deny`, never `require_approval`. A caller that ever receives any other outcome treats it as `deny`.
3. **The write's approval rule is not the verify operation's decision.** The tool's policy rule is consulted only for an explicit refusal: a `deny` rule, or no rule at all (deny by default), refuses the probe. An `allow` or `require_approval` rule neither grants nor refuses it; the permission checks of rule 6 decide. The approval that authorized the write is never read as authority for the probe (owner: "reuse the approval as blanket authority" is forbidden).

### Relation to ADR-0019 rule 8

4. **This clarifies rule 8; it does not amend it.** Rule 8 evaluates policy "for the verify operation" and never says that operation inherits the write tool's rule; this ADR defines what "the verify operation" is for policy. Everything else in rule 8 still holds exactly: verification is attempted only for an entry that reached `indeterminate` through TX7, TX6r or TX12 after a TX5-authorized write; no new decision on the write is made; the inputs are the TX5 trusted inputs; the active policy version is read in the same transaction (TX-V1); the decision set is `allow`/`deny` and `require_approval` as a verify decision is `deny`; a `deny` ends automatic verification and hands over to human reconciliation; the pinned version must still resolve. In particular **an explicit `deny` rule on the tool, a stale or changed policy version, and a failed permission check all refuse the probe.** ADR-0021 rule 18 is resolved by this ADR.

### The seven conditions as enforceable rules

5. **Conditions 1, 2 and 7 (read-only, predeclared in the exact approved version, no new action).**
   - The only code the framework ever calls during verification is the `probe` of the `VerifyDeclaration` on the descriptor resolved by `ToolSource.resolve(tool, tool_version)` with the ledger entry's pinned version (ADR-0021 rule 14). Never a newer version, never the tool's `handler`, never another tool.
   - A pinned version that no longer resolves, or a resolved descriptor with no `verify`, is not verified.
   - `evaluate_verification` refuses unless the evaluated tool is a `write` with a declaration (`verify_not_declared`) and its version equals the entry's pinned version (`version_mismatch`).
   - The probe receives no handler, transport or credential from Tether; it is the tool author's read-back. Read-only behaviour is the SDK contract (ADR-0019 rule 7), proven by a no-mutation contract test against each reference target; it cannot be proven for arbitrary code, and the guarantee card says so.
6. **Conditions 5 and 6 (independently authorized for a read purpose; no permission gain).**
   - The probe needs `VerifyDeclaration.required_permissions`. The requester's permissions (union of `role_permissions` over the run's requester snapshot) intersected with the run agent's permissions, under the **current** policy, must cover every one of them (`agent_lacks:<p>`, `requester_lacks:<p>`).
   - **The current policy version must be the version that authorized the write** (the `policy_version` of the entry's `EXECUTION_AUTHORIZED` event). The requester snapshot and the agent are immutable for a run, so under the same policy version the effective permissions now are exactly those that authorized the write: the probe can only use permissions the original effective context had. A changed version refuses the probe (`policy_changed`), even if the new version would grant it; Tether stores no earlier policy content to compare against, so a changed policy hands the entry to a person. A missing `EXECUTION_AUTHORIZED` version refuses it as well (`authorization_unknown`).
   - The caller refuses before evaluating when this process's loaded policy is not the active version (`policy_stale`), as TX5 does; unlike TX5 it does not retry, it hands over to reconciliation.
7. **Condition 3 (only the exact original action, target and receipt).**
   - The probe receives only the original call's validated arguments and a `VerifyContext` holding the entry's idempotency key. V1 ledger entries record no target receipt, so none is passed.
   - Before every probe the framework re-validates the stored arguments with the pinned input model and recomputes the keyed call hash under the run's binding key (ADR-0010); `evaluate_verification` refuses unless it equals the ledger entry's `call_hash` (`target_mismatch`). Model output, new targets or other arguments can therefore never reach a probe.
8. **Condition 4 (same requester, agent, workspace and policy context).** Requester, agent and workspace come from the run row (requester snapshot, agent, workspace) and the ledger entry, never from the model or a request. `evaluate_verification` refuses on another workspace (`workspace_mismatch`), a missing requester subject (`context_missing:requester`) or an agent with no permissions entry (`context_missing:agent`). The policy context is pinned by rule 6.

### What happens when a condition fails

9. **Any refusal ends automatic verification honestly.** A refusal by rules 5–8, `policy_stale`, an unresolvable pinned version, an unconfigured agent or workspace, a recorded `REPLAY_DIVERGENCE` on the run, an active stop and exhausted attempts each end automatic verification in TX-V1 with `VERIFICATION_EXHAUSTED` and the reason, plus a wake; the entry stays `indeterminate` and the run shows `awaiting_reconciliation` (ADR-0019 rule 11). An active stop additionally records `VERIFICATION_SKIPPED` with reason `stopped` in the same transaction (ADR-0019 rule 9), so both of ADR-0019's statements about a stop hold. A policy refusal is an observation event, never a model action, and does not count toward the denial bound (ADR-0019 default 1).
10. **No declaration means no verification at all.** A tool without `verify` gets no `verify_outcome` job and no verification event; its `indeterminate` entry goes straight to human reconciliation (ADR-0019 rule 7, last sentence). ADR-0019 rule 11's "the transaction that ends automatic verification" applies only where verification began, i.e. a `verify_outcome` job exists.
11. **Result vocabulary unchanged.** A probe returns `confirmed_applied`, `confirmed_not_applied` or `inconclusive` with evidence, exactly as ADR-0019 rule 10 states. An exception, a timeout (the tool's `timeout_seconds`, ADR-0021 rule 17) and evidence that does not validate against `evidence_model` are `inconclusive`. Only a definitive result resolves the entry (TX-V2); no definitive result after the bound leads to human reconciliation. Evidence is masked through the ADR-0017 choke point before it is persisted, audited or shown to the model.

## Alternatives Considered

### Alternative 1: The verify operation inherits the write tool's rule (ADR-0021 rule 18's reading)
- **Pros**: No new decision point.
- **Cons**: Every approval-gated write, which is every high-risk write, could never be verified; verifying the writes that matter most would always need a person.
- **Why not**: The owner decided that a read-back inside the original context needs no second approval.

### Alternative 2: A separate policy rule per verify operation in bootstrap
- **Pros**: Operators could switch verification on or off per tool.
- **Cons**: A new bootstrap schema and policy-version input; a rule could grant the probe permissions the write never had.
- **Why not**: The declared permissions plus the requester × agent intersection already express it, and condition 6 is easier to prove without a second rule source. Revisit if operators ask for it.

### Alternative 3: Store the write's effective permissions on the ledger entry
- **Pros**: Verification could continue across a policy change.
- **Cons**: A schema change ADR-0019 does not name; a stored permission set is a second, unversioned copy of policy.
- **Why not**: Requiring the authorizing policy version keeps condition 6 exact with no new state; a policy change during an unknown outcome is rare and a person handles it.

### Alternative 4: Treat the write's approval as authorizing the probe
- **Pros**: No new evaluation.
- **Cons**: Reuses an approval as blanket authority, which the owner forbade; ignores policy changes after the approval.
- **Why not**: Forbidden by the owner's decision.

## Rationale

The owner's seven conditions describe a probe that is strictly smaller than the action already governed: the same people, the same agent, the same arguments, the same policy version, fewer permissions, and no effect. A dedicated decision point makes "independently authorized" a checkable fact instead of a convention, and pinning the authorizing policy version is the simplest exact proof of "no permission gain". Every refusal leads to the existing human path, so nothing new can be inferred without evidence.

## Consequences

### Positive
- Approval-gated writes can be verified without a second approval and without reusing the first one.
- Each of the seven conditions is a named refusal reason in the audit trail.

### Negative
- `PolicyEngine` grows a fourth method; future adapters (OPA, Cedar) must implement it.
- Any policy deployment between a write and its probe disables automatic verification for that entry.
- Tool authors must list the probe's permissions and grant them to requester roles and agents; a probe needing a permission nobody holds is never allowed.

### Risks
- A probe that mutates the target despite the contract. Mitigation: the SDK contract, review, and the no-mutation contract tests (ADR-0019 Consequences).

## Failure and Recovery

- Stale policy in this process during a bootstrap rollout: verification ends with `policy_stale`; a person reconciles.
- Crash after TX-V1 and before the probe: the job is redelivered; the gate re-checks every rule above and counts a new attempt against the bound.
- Policy unreadable (database down): nothing progresses; nothing is inferred.

## Invariants and Constraints

- §22 invariants 11 (policy inputs never from model output), 36 and 37 (ADR-0019) hold: no probe starts without a committed gate that observed the stop predicate false and an `allow` from `evaluate_verification` under the active version that also authorized the write.
- A probe never receives anything but the hash-checked original arguments and the idempotency key; it never calls the handler.
- Separation of duties (ADR-0020 rule 5) is unaffected: verification is a system actor (`system:verification`), never an approver or a reconciler.

## Revisit When

- Ledger entries record a target receipt (pass it to the probe).
- Operators need per-tool switches for verification, or verification across policy changes.
- OPA or Cedar adapters are built.

## References

- PRD (revision 2026-11-09): Outcome verification and reconciliation; T-27.
- Architecture: §12.2.1, §13.1, §22 invariants 11, 36, 37.
- ADRs: [ADR-0009](0009-policy-engine-current-policy.md), [ADR-0010](0010-keyed-canonical-call-hash.md), [ADR-0011](0011-tool-version-pinning-approval-validity.md), [ADR-0017](0017-sensitive-data-masking-choke-point.md), [ADR-0019](0019-automatic-verification-of-unknown-outcomes.md), [ADR-0020](0020-v1-policy-engine-contract.md), [ADR-0021](0021-v1-tool-contract.md).
- Plan: W5.1.
