# ADR-0004: Postgres job queue: at-least-once delivery, per-event wake jobs, delayed-job timers, dead-letter transitions

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-08 (queue parts); §11.4; §24 items 8 (part), 15 (part); review findings F-01, F-05

## Context

Dispatching work must be transactional with ledger and audit writes. The PRD requires at-least-once delivery with a bounded number of attempts, and timers for approval expiry and the 7-day run deadline that hold no compute. ADR-0001 rules out Redis and brokers. The architect review found two hazards. First, coalescing wake jobs can lose a wake-up and strand a run (F-01). Second, a job that exhausts its attempts without a domain transition strands the run (F-05).

## Decision

1. Jobs live in a **Postgres job table behind a `JobQueue` interface**. Enqueue happens inside the caller's transaction, so the job table is its own transactional outbox.
2. **Delivery is at-least-once.**
   - Jobs are leased with `SKIP LOCKED`, a lease token and expiry, and heartbeats; completion is conditional on the lease token.
   - Exactly-once delivery is never assumed. Each job kind is safe to redeliver under its own domain rules:
     - run driving: ADR-0003 and ADR-0005;
     - tool execution: ADR-0007 and ADR-0008;
     - timers: idempotent conditional updates.
3. **Wake-up is by polling only in V1.** `LISTEN/NOTIFY` may be added later as a latency hint, but correctness must never depend on a notification arriving.
4. **No coalescing.** Every state-changing event transaction (TX4, TX7, TX8, TX9, TX10, TX12) inserts its own wake job, which cannot be leased before that transaction commits. Duplicate wake jobs are harmless no-ops (ADR-0003).
5. **Bounded delivery.** When a job's attempts exceed its maximum, the job is marked dead **in the same transaction** as a kind-specific domain transition (TX12) plus a wake. Exhaustion always produces a domain outcome.
6. **Timers are delayed jobs** (`run_after`) in the same queue. Approval expiry and the stored run deadline are also **enforced at decision time against the database clock**. A late timer only materialises state (expired approval, deadline marker, closed approvals) and wakes the run.

## Alternatives Considered

### External broker (Redis, RabbitMQ)
- **Pros**: Lower latency and less database load at scale.
- **Cons**: A new dependency; enqueue is no longer atomic with ledger and audit, so an outbox relay is needed.
- **Why not**: Rejected in ADR-0001.

### `LISTEN/NOTIFY` as the primary delivery mechanism
- **Pros**: Near-zero latency.
- **Cons**: Notifications are lost while no listener is connected; delivery is not durable.
- **Why not**: Correctness cannot depend on it. It stays an optional optimisation.

### Coalesced wake jobs (one ready job per run)
- **Pros**: Less duplicate work.
- **Cons**: An `ON CONFLICT DO NOTHING` insert can find a ready row that a concurrent lease consumes before the event commits, so the wake is lost (F-01).
- **Why not**: It can strand runs.

### Cron sweeper for timers; timer-only enforcement
- **Pros**: Familiar pattern.
- **Cons**: Duplicates leasing logic. If timers are the only enforcement, expiry and deadlines are late whenever workers are down.
- **Why not**: Delayed jobs plus decision-time checks are simpler and always correct.

### Dead-letter without a domain transition
- **Pros**: Generic queue code.
- **Cons**: Runs wait forever on work that will never happen (F-05).
- **Why not**: Exhaustion must produce an honest outcome.

## Rationale

The queue is part of the same consistency domain as the ledger and audit. At-least-once delivery is made safe per job kind instead of being wished away. Every way out of the queue — success, exhaustion, timer firing — produces a domain transition and a wake.

## Consequences

### Positive
- Intent and dispatch are atomic, with no extra infrastructure.
- Timers survive restarts and hold no compute (P-03).
- Exhaustion can never strand a run.

### Negative
- Polling adds latency and database load.
- Duplicate wake jobs cause extra no-op invocations.
- The job table needs cleanup, which depends on the PRD's open data-retention question.
- Replacing the queue later requires an outbox relay.

### Risks
- A handler that is not redelivery-safe would break the model. Mitigation: per-kind rules in ADR-0003, ADR-0005, ADR-0007, ADR-0008, plus chaos tests C-04 to C-07.

## Failure and Recovery

- Worker crash: the lease expires and the job is redelivered under the per-kind rules.
- Database down: nothing progresses, nothing is lost.
- Exhaustion (TX12):

| Job kind | State at exhaustion | Result |
|----------|---------------------|--------|
| `execute_tool` | `pending` | `failed` (not applied) |
| `execute_tool` | `dispatched`, write | `indeterminate` |
| `execute_tool` | `dispatched`, read | typed failure |
| `advance_run` | ledger entry in flight or `indeterminate` | run moves to awaiting reconciliation |
| `advance_run` | otherwise | run `failed` with reason `system_error` |

## Invariants and Constraints

- §22 invariants 16, 23, 25, 29. The queue also supports invariants 1 and 4.
- The queue must never be assumed exactly-once, and wake jobs must never be coalesced.

## Revisit When

- Throughput or latency targets outgrow polling.
- External consumers need events.
- Job-table growth becomes an operational problem, or a data-retention policy is set.

## References

- PRD: Dispatch and side-effect outcome semantics (at-least-once, bounded delivery); Core Capabilities (worker queue); Open Questions (API ↔ worker transport, resolved); Decisions Log (Queue delivery, API ↔ worker transport); tests T-15, C-07, P-03.
- Architecture: §11 AD-08, §11.4, §13.1 (TX4, TX7–TX10, TX12), §20, §21 Q2, §27 F-01, F-05, simplifications S-*.
