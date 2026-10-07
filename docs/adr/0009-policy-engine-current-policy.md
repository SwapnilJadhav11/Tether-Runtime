# ADR-0009: Policy engine, current-policy definition, and re-evaluation before execution

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-05; §13.1 TX2/TX4/TX5/TX8; §24 items 5, 20 (A-01); review finding F-07
**Resolves**: A-01

## Context

The PRD requires:
- a deny-by-default `PolicyEngine` (built-in now, Cedar/OPA later);
- approver and reconciler eligibility evaluated by policy over both the actor's and the requester's identities;
- **current policy re-evaluated before execution, with a deny overriding an approval** (T-19).

Policy is loaded from bootstrap config and includes workload rules written in code. `api` evaluates eligibility and `worker` evaluates actions, so during a deployment different processes can hold different loaded policy. Open question A-01 asks what "current" means across those processes.

## Decision

1. **One `PolicyEngine` interface with three decision points:**
   - action: `allow` / `deny` / `require_approval`, with reasons and the policy version;
   - approval eligibility: approver, requester, approval;
   - reconciliation eligibility: reconciler, requester, ledger entry.
   The built-in evaluator is deny-by-default over core rules plus workload rules registered through the SDK. Domain rules such as separation of duties live in workload policy, not in core.
2. **Policy inputs come only from** the registry descriptor, bootstrap policy, and validated identity (the requester snapshot). Model-supplied fields are never inputs. An evaluator exception or missing policy produces `deny` with an audited reason.
3. **Evaluation points:**
   - at proposal (TX2);
   - **again in TX5, before the ledger row is created**, with this mapping:
     - `deny` → typed denied result, no ledger row; this overrides any approval;
     - `require_approval` → proceed only with a matching approved approval (equal call hash, still within its validity window per ADR-0011); otherwise request approval;
     - `allow` → proceed;
   - at approval decision (TX4);
   - at reconciliation (TX8).
   Every decision records its policy version.
4. **Policy versioning.** Each workspace's effective rule set — declarative policy config plus the declared versions of the evaluator and the workload rule modules — has a content hash. That hash is the `policy_version`. Bootstrap apply records the versions and marks exactly one **active** version per workspace.
5. **A-01: current policy = the workspace's active `policy_version` row in Postgres at the moment of the decision transaction.** Each `api` and `worker` process computes the versions of its loaded rule sets at startup and refuses to start if they don't match the active rows. Processes also re-check periodically.
6. Every policy evaluation that produces a decision or an eligibility answer reads the active version **inside the decision transaction**, under a share lock so that a concurrent activation serialises with it, and compares it with the loaded version. **On a mismatch, no decision is recorded:**
   - a worker rolls back, returns the job to ready without consuming an attempt, stops leasing, and exits to be restarted with current configuration;
   - `api` returns a retryable error (HTTP 503) with no state change, stops serving decisions, and exits to be restarted.
7. **A policy change is a deployment:** change config or workload code, run bootstrap apply (which activates the new version), restart processes. There is no hot reload and no runtime policy API (ADR-0014).

**Deviation from the §24 recommendation.** The recommendation said mismatched processes should *deny*. This ADR has them *record no decision* instead. A deny is a decision-class audit event that is adopted on replay (invariant 27), fed to the model as a typed denial, and counted against the denial budget. That would turn a deployment transient into a permanent, model-visible policy outcome. Refusing to decide is just as fail-closed (no side effect, no approval, no reconciliation). It reuses existing mechanisms: requeue without consuming an attempt (§9.3) and "all workers down → runs pause" (AD-01). No component, transaction, or PRD guarantee changes.

## Alternatives Considered

### Each process's loaded policy is "current"
- **Pros**: No coordination.
- **Cons**: After a policy is tightened, a stale process keeps applying the older, more permissive rules.
- **Why not**: It defeats "current policy re-evaluated before execution" (T-19).

### Store rules in the DB and hot-reload
- **Pros**: Instant policy changes.
- **Cons**: Workload rules are code; it would need a runtime admin surface.
- **Why not**: Contradicts the no-admin-API decision and the SDK-based rule model.

### On version mismatch, deny
- **Pros**: Simple.
- **Cons**: Persists a spurious, replay-adopted denial and consumes the denial budget.
- **Why not**: See the deviation note above.

### Cedar/OPA now; eligibility hard-coded in endpoints
- **Pros**: A policy language, or less abstraction.
- **Cons**: Cedar/OPA is deferred by the PRD. Hard-coding bakes domain rules (separation of duties) into core.
- **Why not**: Both conflict with the PRD.

## Rationale

The database is the only state shared by all processes, so the active version recorded there is the only unambiguous definition of "current". Comparing it inside the decision transaction ensures no decision is ever made under a superseded policy. Mapping a mismatch to "no decision" keeps the decision record truthful.

## Consequences

### Positive
- "Current policy" is well defined across `api` and `worker`, and T-19 is deterministic.
- Every decision can be attributed to a version.
- A Cedar/OPA migration maps onto the three decision methods.

### Negative
- Every policy change needs bootstrap apply plus a restart, and decisions pause briefly during rollout.
- A mis-deployed process crash-loops; this is loud, by design.
- One extra indexed read per decision, which is small relative to the P-01 budget.

### Risks
- An active-version activation could deadlock with decision transactions. Mitigation: activation takes only the policy-version row lock; its position in the global lock order is a planning detail.

## Failure and Recovery

- **Mismatch:** no decision; runs pause until an up-to-date process handles them. If every process is stale, nothing progresses and nothing is lost.
- **Evaluator error:** audited `deny`.

## Invariants and Constraints

- §22 invariants 11, 26, 27.
- New invariant **31** (added to architecture §22). A policy decision or eligibility answer is recorded only if the evaluating process's loaded version equals the workspace's active version, read in the same transaction.

## Revisit When

- Cedar/OPA is adopted.
- A post-V1 admin API, or a requirement for policy changes without restarts, appears.
- Per-workspace budget enforcement through policy is built.
- OIDC-backed mid-run revocation is added.

## References

- PRD: Core Capabilities (PolicyEngine; approval gate with current-policy re-evaluation); Identity over the life of a run; Threat model; Decisions Log (Policy engine, Identity over time, Self-approval); tests T-06, T-07, T-19, T-22, T-23, T-25; P-01.
- Architecture: §6 AD-05, §13.1 (TX2, TX4, TX5, TX8), §18 (policy versions written by the bootstrap CLI), §24 items 5, 20, §27 F-07, A-01.
