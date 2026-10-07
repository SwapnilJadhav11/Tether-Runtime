# ADR-0011: Tool-version pinning across deployments and approval validity window

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: §7; §12.1; §13.1 TX5–TX6; §14; §24 item 23 (A-04)
**Resolves**: A-04

## Context

The call-hash envelope includes the tool version, ledger rows store it, and `ToolSource` resolves `(name, version)`. Runs can last 7 days and approvals can wait until their expiry (24 h by default). A deployment in between may change or remove a tool version. An approval given for one version's behaviour must never end up executing another version's code. Separately, after a worker outage, an approved but not yet authorized call could sit for a long time before TX5 runs.

## Decision

1. **The version is pinned at proposal.** The model names a tool. Governance resolves the version currently offered under that name in the workspace, and that version enters the hash envelope, the approval, the decision events, and the ledger. **Exactly that version executes, or nothing does.** Versions are never silently upgraded or downgraded.
2. **What the registry offers.**
   - Models see exactly one current version per tool name.
   - A workload may keep earlier versions registered as resolvable but not offered, so that in-flight calls can drain across a deploy.
   - Tool authors must bump the version on any change to input or output schema, effect, idempotency support, preconditions, or execution semantics.
3. **At authorization (TX5, before a ledger row exists).** If the pinned version doesn't resolve in the authorizing process, the call gets a typed `not_applied` result (reason: tool version unavailable). That result is recorded as a decision-class audit event, and no ledger row is created and no job enqueued. If it does resolve, policy is re-evaluated against that version's current descriptor.
4. **At first dispatch (before TX6).** If the executing worker cannot resolve the pinned version, the entry goes `pending → failed` (not applied; nothing left Tether). This is the same path as the payload-hash check.
5. **After dispatch.** If a re-send is needed but the version no longer resolves, nothing is re-sent. The outcome is unknown, so a write becomes `indeterminate` and a read gets a typed failure. It is never `not_applied`, because the first attempt may have applied.
6. **Approval validity window.**
   - An approved approval authorizes TX5 **only while the database clock is before the approval's `expires_at`**. This is the same per-tool expiry that bounds the decision itself.
   - After that, TX5 returns a typed `expired` result (the approval lapsed before use) and creates no ledger row; a re-proposal needs a fresh approval.
   - An approval does **not** remain valid until the run deadline.
   - Within the window, current-policy re-evaluation still applies (ADR-0009).
   - Once a ledger row exists, the ledger-first rule applies: validity is not re-checked at dispatch, and only the stop predicate gates TX6.

## Alternatives Considered

### Execute the latest registered version
- **Pros**: Calls survive deploys.
- **Cons**: The approval was for different code; this violates the intent of T-08.
- **Why not**: Approved behaviour must equal executed behaviour.

### Fail the run when the version disappears
- **Pros**: Simple.
- **Cons**: Too harsh, when the model could simply re-plan.
- **Why not**: A typed `not_applied` result keeps the run useful and honest.

### Hold the call until a worker with that version appears
- **Pros**: No lost approvals during rolling deploys.
- **Cons**: Unbounded wait once old workers are gone.
- **Why not**: Not deterministic.

### Approval valid until the run deadline, subject to re-evaluation
- **Pros**: Fewer lapsed approvals after outages.
- **Cons**: Stretches the approver's judgement across up to 7 days. Preconditions cover only declared fields. The PRD bounds exposure by approval expiry and maximum duration.
- **Why not**: Bounding use by the same expiry is more conservative and adds no new parameter.

### A separate execution-window setting; re-checking expiry at TX6
- **Pros**: Finer control.
- **Cons**: An extra knob with no requirement behind it. Re-checking at TX6 breaks ledger-first and turns authorized rows into failures after a crash between TX5 and TX6.
- **Why not**: Unnecessary, and unsafe under replay.

## Rationale

Pinning makes the binding cover code as well as arguments. Making each outcome follow from what is actually known (nothing sent → `not_applied`; possibly sent → `indeterminate`) keeps the honesty rules intact. Reusing the approval expiry as the usage bound keeps exposure within the limits the PRD already states.

## Consequences

### Positive
- Approved code is executed code; deployments cannot silently change approved behaviour.
- Outcomes are deterministic.
- Exposure is bounded by approval expiry.

### Negative
- A deploy that removes a version turns waiting calls into `not_applied` (or `indeterminate` if already dispatched).
- Workloads that want calls to drain across deploys must keep earlier versions resolvable.
- A worker outage longer than the expiry lapses approvals, which must then be re-requested.

### Risks
- Tests that combine very short expiries with a crash before TX5 may see `expired`. Mitigation: test design for C-04 and T-15.

## Failure and Recovery

- During a mixed-version rollout, whichever process authorizes or leases the job decides deterministically from its own registry.

## Invariants and Constraints

- §22 invariants 4, 5, 15, 16, 22, 26.
- New invariant **33** (added to architecture §22). A call executes only under its pinned version: if unresolvable before dispatch it is `not_applied`; if needed for a re-send after dispatch, a write becomes `indeterminate`. An approved approval authorizes TX5 only before its `expires_at`.

## Revisit When

- MCP tool sources with server-side versioning are added.
- Long-lived approvals become a requirement.
- Blue/green deployments with version-aware job routing are introduced.

## References

- PRD: Core Capabilities (approval gate: per-tool expiry, current-policy re-evaluation); Identity over the life of a run (exposure bounded by approval expiry and maximum duration); Dispatch and side-effect outcome semantics; failure paths F4, F6; tests T-08, T-15, T-19, C-04.
- Architecture: §7, §11.4, §12.1, §12.4, §13.1 (TX5, TX6), §14, §24 item 23, §27 A-04.
