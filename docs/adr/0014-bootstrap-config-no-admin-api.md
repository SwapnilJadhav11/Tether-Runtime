# ADR-0014: Declarative bootstrap configuration and CLI as the only platform-administration surface

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: §4 (cli, config/bootstrap), §18, §19; §24 item 18

## Context

The PRD states that V1 has no platform-admin HTTP API and no runtime platform-admin role. Workspaces, trusted issuers, workspace API keys, and agent bindings are managed through declarative configuration plus a CLI, and the architecture chooses the format. Policy versions also need one shared record so "current policy" can be defined (ADR-0009).

## Decision

1. Workspaces, trusted issuers and their *public* verification keys, API-key hashes, agent identities and bindings, workload enablement, and policy sets are declared in **version-controlled bootstrap configuration**.
2. A **CLI applies the configuration to Postgres** idempotently and transactionally. It runs as a one-shot step before `api` and `worker` start, records policy versions, and activates one per workspace (ADR-0009).
3. There is **no admin HTTP API and no runtime platform-admin role**. The platform administrator is an operator with deployment access.
4. **Changes take effect through apply plus a restart.** Processes never hot-reload configuration.
5. Secrets (HMAC keys, provider API keys) come from `SecretsProvider`, never from bootstrap files. Only salted hashes of API keys are stored; how raw keys are issued is a planning detail.

## Alternatives Considered

### Admin HTTP API
- **Pros**: Self-service and automation.
- **Cons**: A privileged runtime surface that needs its own authentication and authorization model.
- **Why not**: An explicit PRD non-goal for V1.

### Manual SQL
- **Pros**: No tooling to build.
- **Cons**: Not reviewable, not idempotent, error-prone.
- **Why not**: Unsafe for identity and policy data.

### Each process reads config files directly, with no database record
- **Pros**: No apply step.
- **Cons**: No shared truth across processes for workspaces or the active policy.
- **Why not**: ADR-0009 needs an active version in the database.

## Rationale

Configuration as code gives reviewable, reproducible administration with no privileged runtime surface. The database copy gives every process one shared truth.

## Consequences

### Positive
- No admin attack surface at runtime.
- Configuration changes are code-reviewed and reproducible in Compose.

### Negative
- Every change is a deployment, and there is no product-team self-service (consistent with tier-1 slip of workspace-management UX).
- Rotation procedures are runbooks.

## Failure and Recovery

- If apply fails, the one-shot step fails and `api`/`worker` don't start. Because apply is transactional, configuration is never partially applied.

## Invariants and Constraints

- §22 invariants 11, 13.
- No runtime endpoint may create or alter workspaces, issuers, API keys, agent bindings, or policy.

## Revisit When

- The platform stage requires self-service or an admin API.
- OIDC or Vault integration changes where keys and identities come from.

## References

- PRD: Trust model ("Platform administration has no runtime API in v1"); What We're NOT Building (platform-admin HTTP API); Decisions Log (Platform administration, Roles); Phase 1 scope; tests T-18, T-24.
- Architecture: §4 (cli, config/bootstrap), §5 AD-03, §6 AD-05, §18, §19, §24 item 18.
