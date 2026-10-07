# Architecture Decision Records

Architecture Decision Records (ADRs) for Tether V1. Each ADR records one significant, hard-to-reverse decision: its context, the alternatives considered, the rationale, and the consequences.

- Requirements baseline (frozen): [`../../.claude/PRPs/prds/tether-runtime.prd.md`](../../.claude/PRPs/prds/tether-runtime.prd.md)
- Reviewed architecture: [`../architecture/tether-v1-architecture.md`](../architecture/tether-v1-architecture.md)
- Lifecycle: `proposed` → `accepted` → `deprecated` | `superseded by ADR-NNNN`. A superseded ADR always links its replacement. New ADRs use [`template.md`](template.md) and take the next number.

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-two-process-topology-postgres-only.md) | Two Tether processes from one image on a Postgres-only footprint | accepted | 2026-10-06 |
| [0002](0002-core-sdk-workload-layering.md) | Core / SDK / workload layering with machine-checked import contracts | accepted | 2026-10-06 |
| [0003](0003-authoritative-tables-checkpoint-progress-cache.md) | Tether tables are authoritative; the LangGraph checkpoint is a replay-safe progress cache | accepted | 2026-10-06 |
| [0004](0004-postgres-job-queue.md) | Postgres job queue: at-least-once delivery, per-event wake jobs, delayed-job timers, dead-letter transitions | accepted | 2026-10-06 |
| [0005](0005-run-driver-mutex-and-fencing.md) | Run-driver mutual exclusion: session advisory lock plus per-run fencing token | accepted | 2026-10-06 |
| [0006](0006-run-lifecycle-single-status-writer.md) | Run lifecycle: single status writer, stop predicate, terminal immutability | accepted | 2026-10-06 |
| [0007](0007-execution-ledger-honest-outcomes.md) | Execution ledger, honest outcome classification, and human-only resolution of `indeterminate` | accepted | 2026-10-06 |
| [0008](0008-idempotency-keys-and-stop-gated-resends.md) | Deterministic idempotency keys and stop-gated re-sends of in-flight writes | accepted | 2026-10-06 |
| [0009](0009-policy-engine-current-policy.md) | Policy engine, current-policy definition, and re-evaluation before execution | accepted | 2026-10-06 |
| [0010](0010-keyed-canonical-call-hash.md) | Approval binding: RFC 8785 canonical envelope with keyed (HMAC) call hash | accepted | 2026-10-06 |
| [0011](0011-tool-version-pinning-approval-validity.md) | Tool-version pinning across deployments and approval validity window | accepted | 2026-10-06 |
| [0012](0012-sequential-multi-call-governance.md) | Sequential governance of multi-call model turns with typed `skipped` remainder | accepted | 2026-10-06 |
| [0013](0013-identity-trust-boundary-workspace-isolation.md) | Identity trust boundary: route-bound audiences, platform-controlled issuers, workspace-scoped data access | accepted | 2026-10-06 |
| [0014](0014-bootstrap-config-no-admin-api.md) | Declarative bootstrap configuration and CLI as the only platform-administration surface | accepted | 2026-10-06 |
| [0015](0015-per-run-hmac-audit-and-authoritative-reconstruction.md) | Per-run HMAC audit chain with write-ahead critical events; Postgres is the reconstruction record | accepted | 2026-10-06 |
| [0016](0016-provider-neutral-history-and-fallback.md) | Provider-neutral history with capability-qualified, per-turn fallback | accepted | 2026-10-06 |
| [0017](0017-sensitive-data-masking-choke-point.md) | Sensitive-data masking at a single choke point | accepted | 2026-10-06 |

## Mapping from architecture §24 (ADR candidates)

| §24 item | Candidate | ADR |
|----------|-----------|-----|
| 1 | AD-01 Process topology | 0001 |
| 2 | AD-02 Core / SDK / workload layering | 0002 |
| 3 | AD-03 Route-bound audiences, issuer keys, API-key agent binding | 0013 |
| 4 | AD-04 Workspace-scoped repositories (RLS deferred) | 0013 |
| 5 | AD-05 PolicyEngine decision points and policy versioning | 0009 |
| 6 | AD-06 Provider-neutral history and fallback | 0016 |
| 7 | AD-07 Run lifecycle | 0006 |
| 8 | AD-08 Postgres job queue and advisory-lock mutex | 0004 (queue), 0005 (mutex) |
| 9 | AD-09 Deterministic idempotency keys | 0008 |
| 10 | AD-10 Authoritative tables vs checkpoint progress cache | 0003 |
| 11 | AD-11 Canonical call hash | 0010 |
| 12 | AD-12 Per-run HMAC audit chain | 0015 |
| 13 | AD-13 Authoritative reconstruction and trace context | 0015 |
| 14 | AD-14 Sensitive-data masking choke point | 0017 |
| 15 | Dispatch-and-interrupt waiting model | 0003 |
| 16 | Sequential governance of multi-call responses | 0012 |
| 17 | Outcome classification and late-result evidence | 0007 |
| 18 | Bootstrap configuration + CLI as only admin surface | 0014 |
| 19 | Run-driver wake protocol and `durability="sync"` | 0003 (durability, check-before-interrupt, payloads), 0005 (lease-then-lock) |
| 20 | A-01 Current policy | 0009 |
| 21 | A-02 Zombie run-driver fencing | 0005 |
| 22 | A-03 Multi-call remainder | 0012 |
| 23 | A-04 Tool version and approval window | 0011 |
| 24 | A-05 Keyed call hash | 0010 |
| 25 | A-06 Post-stop idempotent redelivery | 0008 |
