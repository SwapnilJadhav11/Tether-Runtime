# ADR-0020: Clarification of ADR-0009: the V1 policy engine contract, mandatory separation of duties and trusted policy inputs

**Date**: 2026-11-15
**Status**: accepted (2026-11-15; proposed and accepted under owner decision U-008, W3.1)
**Deciders**: Tether project owner (SwapnilJadhav11)
**Architecture source**: AD-05 (§6); invariants 11, 31; PRD Core Capabilities "Stable `PolicyEngine` interface"; owner decisions OD-5, U-008 (2026-11-15)

## Context

ADR-0009 fixes one `PolicyEngine` with three decision points (action, approval eligibility, reconciliation eligibility), current-policy evaluation at TX2 and again at TX5, and a content-hashed `policy_version`. It leaves the V1 rule model open, and its rule 1 says the built-in evaluator runs "core rules plus workload rules registered through the SDK", with "domain rules such as separation of duties" living "in workload policy, not in core". No SDK rule mechanism exists, separation of duties has always been enforced by the built-in evaluator, and the owner decided (U-008, 2026-11-15) the exact V1 contract. Accepted ADRs are not edited, so this ADR records the clarification and the one amendment to ADR-0009 rule 1.

## Decision

The V1 policy engine is a declarative, built-in evaluator behind a stable protocol; separation of duties is a built-in invariant; only trusted, runtime-owned values are policy inputs.

1. **Protocol.** `PolicyEngine` has exactly three methods, each pure and synchronous, bound to one workspace's loaded policy: `evaluate_action(ActionRequest) → PolicyDecision`, `evaluate_approval_eligibility(ApprovalEligibilityRequest) → Eligibility`, `evaluate_reconciliation_eligibility(ReconciliationEligibilityRequest) → Eligibility`. Reading and comparing the active `policy_version` stays in the caller's decision transaction (ADR-0009 rules 5–6, unchanged). OPA or Cedar, post-V1, are adapters of the same protocol.
2. **V1 policy is declarative configuration.** The built-in evaluator reads only the workspace's bootstrap `policy` section: one rule per tool (`allow` / `require_approval` with approver roles / `deny`), `role_permissions`, `agent_permissions` and reconciler roles. **SDK-registered policy rule code is deferred** past V1; this amends the corresponding part of ADR-0009 rule 1. `policy_version` hashes the declarative policy together with the built-in evaluator's version (ADR-0009 rule 4).
3. **Permissions.** Requester permissions are the union of `role_permissions` over the requester's snapshotted roles; agent permissions are `agent_permissions[agent]`; the effective set is their intersection, and every required permission of the tool must be in it. Permissions never come directly from token claims. Unknown roles grant nothing; an agent with no entry has no context to decide on, which is a deny.
4. **Risk** is fixed tool metadata, exactly `low` or `high`, with no numeric scoring. A high-risk tool is never allowed automatically: a configuration that `allow`s one is refused at worker startup, and the evaluator returns `deny` if such a rule is ever reached.
5. **Separation of duties is a built-in, non-disableable invariant** of the evaluator, not workload policy (this amends ADR-0009 rule 1's sentence on separation of duties): an approver is never the run's requester, and a reconciler is never the run's requester.
6. **Trusted inputs only.** Requester (subject and roles from the run's snapshot of the validated token), agent (the run's agent, bound by the API key), workspace (the run's), tool metadata (the registry descriptor only) and runtime metadata (run, step ordinal, call hash). Raw model text, rationale, raw model arguments and tool output are never policy inputs, including values that merely passed schema validation (invariant 11; ADR-0009 rule 2, unchanged).
7. **Runtime-attested action context.** `ActionContext` carries an immutable `trusted_attributes` mapping of scalar values. It is empty in V1 and the built-in evaluator does not consume it. Only Tether's runtime may populate it, and only from authoritative or explicitly verified sources; copying raw model arguments into it is forbidden. How authoritative resource attributes are produced is left to W3.2 or a later ADR; this rule only keeps the protocol stable for that later work.

All other rules of ADR-0009 (evaluation points, TX5 mapping, current-policy definition, stale-version handling, policy changes as deployments) stand unchanged.

## Alternatives Considered

### Alternative 1: Separation of duties as configurable workload policy (ADR-0009 rule 1 as written)
- **Pros**: Core stays free of a domain rule; a workload could relax it.
- **Cons**: Needs an SDK rule mechanism that does not exist; a misconfiguration could let a requester approve or reconcile their own action.
- **Why not**: The owner treats the separation as a safety invariant of the governed boundary, not a domain preference.

### Alternative 2: Permissions from token claims
- **Pros**: No mapping in bootstrap.
- **Cons**: Permissions would be unversioned, unreviewed and frozen at submission; a revocation could not take effect at TX5.
- **Why not**: Bootstrap maps are versioned with the policy and re-read at TX5.

### Alternative 3: Argument-derived action context in V1
- **Pros**: Resource-aware rules (for example "only this resource").
- **Cons**: Arguments are model-supplied; trusting them as policy input breaks ADR-0009 rule 2 and invariant 11.
- **Why not**: Deferred; `trusted_attributes` keeps the protocol ready for runtime-attested context later.

## Rationale

A small declarative evaluator covers the V1 requirements (deny by default, user × agent intersection, risk-gated approval, explicit deny) and stays reviewable as data. Making separation of duties non-optional removes a class of misconfiguration from the approval and reconciliation boundary. The empty, runtime-owned context field avoids a later breaking change to the protocol without trusting model output now.

## Consequences

### Positive
- Policy is one reviewed, hashed document per workspace; TX5 sees permission and agent revocations.
- Approver and reconciler separation cannot be configured away.

### Negative
- No resource-aware or argument-aware rules in V1.
- A requester's roles stay those snapshotted at submission (mid-run role revocation remains a stated limit).

### Risks
- A workload that needs different separation semantics needs a new ADR.

## Failure and Recovery

Missing trusted context (no requester subject, an agent without permissions, a tool without required permissions) is a `deny` with a recorded reason. A configuration that allows a high-risk tool stops the worker at startup. A stale policy version records no decision (ADR-0009 rule 6, unchanged).
