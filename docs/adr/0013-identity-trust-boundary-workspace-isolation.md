# ADR-0013: Identity trust boundary: route-bound audiences, platform-controlled issuers, workspace-scoped data access

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-03, AD-04; §24 items 3, 4

## Context

The PRD requires:
- three fail-closed credential audiences that are never accepted cross-audience;
- full JWT validation before any policy evaluation;
- issuers controlled by the platform, so the calling application cannot mint approver identities;
- agents resolved server-side from the API key's allowed set;
- roles taken only from validated tokens;
- workspace enforcement on every endpoint and record (never cut), with out-of-scope access returning `404`.

A leaked or misused token must not be able to step up across risk classes.

## Decision

1. **The expected audience comes from the route, never from the token.** The tasks, approvals, and ops routers are each statically bound to one audience, and a token's `aud` must equal its route's audience.
2. **Validation happens before anything else:**
   - pinned signature algorithms;
   - signature against the workspace's platform-controlled issuer keys (an `IssuerKeyProvider`, static from bootstrap in V1);
   - `iss`, `aud`, `exp`/`nbf`, `sub`, and the workspace claim.
   Any failure returns `401` before policy evaluation, audit writes, or tool activity.
3. **Tasks routes also require the workspace API key.** The key is stored as a salted hash and maps to one workspace and a set of allowed agents. The token's workspace claim must match, and the agent is resolved server-side from the allowed set.
4. **Roles come only from the validated token** and are valid only within its workspace. The requester is the submitting subject, not a role, and is snapshotted at submission; the token itself is never stored. Every run requires a human end-user JWT.
5. **Issuer private keys are never deployed with Tether.** Demo tokens come from a dev-only minting tool outside the runtime image, and the demo uses a separate approver key.
6. **Workspace isolation lives in the application's data-access layer.**
   - Request handlers never receive a raw database session. They receive repositories that cannot be built without a workspace scope taken from the principal.
   - Every query filters by workspace. Lookups that hit another workspace, or that the caller is not authorized for, return `404`.
   - Worker code derives its scope from the job and verifies it against the run.
   - Authorization runs in this order: audience → token → workspace → role/ownership → policy eligibility.
7. **Postgres row-level security is deferred** as defence-in-depth behind the repository scope.

## Alternatives Considered

### Read the audience from the token, then authorize by role
- **Pros**: One validation path.
- **Cons**: A high-privilege token works on any endpoint.
- **Why not**: Route binding makes cross-audience reuse impossible by construction (T-19, T-24).

### Trust caller-supplied agent IDs or roles
- **Pros**: Flexible for integrators.
- **Cons**: Defeats the user × agent permission intersection and separation of duties.
- **Why not**: Forbidden by the PRD trust model.

### Row-level security now
- **Pros**: Stronger isolation at the database layer.
- **Cons**: Session-variable plumbing and migration complexity.
- **Why not**: Application scoping, together with the T-24 matrix and the `fixture-b` workspace, is sufficient for V1. RLS remains a recorded hardening seam.

### Return `403` for out-of-scope resources; adopt OIDC now
- **Pros**: Clearer errors; standard identity integration.
- **Cons**: `403` discloses that the resource exists. OIDC is deferred by the PRD.
- **Why not**: Both conflict with the PRD.

## Rationale

Binding audience to route and scope to repository makes the two most dangerous mistakes — accepting the wrong token class, and running an unscoped query — structurally hard rather than merely forbidden by convention.

## Consequences

### Positive
- T-18, T-19 and T-24 hold by construction.
- Moving to OIDC later swaps only the key provider; audiences stay the same.

### Negative
- No mid-run revocation in V1. Exposure is bounded by approval expiry and run duration.
- A compromised issuer is out of scope, as the PRD states.
- Application-level isolation depends on every data path going through scoped repositories.

### Risks
- A raw-SQL path that bypasses repositories. Mitigation: no raw sessions in request handlers, plus the T-24 matrix.

## Failure and Recovery

- An invalid credential returns `401`, out-of-scope access returns `404`, and a missing scope is a construction-time error.

## Invariants and Constraints

- §22 invariants 11, 12, 13.

## Revisit When

- OIDC/JWKS is integrated.
- Service principals or machine-triggered runs are introduced.
- A second real tenant needs database-level isolation (RLS).
- A post-V1 admin API is added.

## References

- PRD: Trust model; Authorization for reads and human actions; Workspace enforcement; Identity over the life of a run; Core Capabilities (identity, authorization); Decisions Log (Identity, JWT validation, Trust model, Credential audiences, Roles, Read-side authorization); tests T-18, T-19, T-24, T-25, T-26.
- Architecture: §5 AD-03, AD-04, §19, §20, §24 items 3, 4.
