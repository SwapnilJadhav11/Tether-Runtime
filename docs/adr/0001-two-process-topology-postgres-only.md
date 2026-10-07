# ADR-0001: Two Tether processes from one image on a Postgres-only footprint

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-01; §24 item 1

## Context

The PRD has five requirements here:
- tools run outside the API process;
- runs survive process restarts;
- work is dispatched through a Postgres-backed, at-least-once queue behind an abstract interface;
- deployment is a single Docker Compose stack;
- the whole thing serves ~10 concurrent runs and ~100 parked approvals within ~$50/month.

Postgres is already required for LangGraph checkpoints, the ledger, and the audit chain. Tether's core safety argument is "no side effect without a committed policy decision, ledger entry, and audit event". That argument is much simpler when those records and the dispatch of work commit in one database transaction. Every extra stateful dependency adds failure modes and splits that transaction.

## Decision

Tether ships as **one image with two entrypoints**:
- **`api`** authenticates and authorizes requests. It records intent (submissions, approval decisions, reconciliations, cancellations) in a transaction, then enqueues work. It never runs graph nodes, calls models, or executes tools.
- **`worker`** leases jobs and runs three job families: run driving (LangGraph invocation, model calls, policy evaluation), tool execution, and timers. It scales by adding replicas.

**One Postgres instance is the only stateful dependency.** It holds all authoritative state: runs, approvals, the execution ledger, the audit chain, the job queue, model-call and cost records, and configuration derived from bootstrap. LangGraph checkpoints live there too, in a schema LangGraph owns. V1 uses **no Redis, no Kafka or other broker, no Temporal, and no Kubernetes**. Deployment is Docker Compose on one host. The reference workload's demo target service is a separate, non-Tether container with its own database.

## Alternatives Considered

### Separate "runner" service for graph driving
- **Pros**: Stronger isolation between run driving and tool code; the two can scale independently.
- **Cons**: An extra container and an extra inter-process hop; no V1 requirement needs it.
- **Why not**: Job-kind routing already lets us deploy workers that lease only some job kinds later, with no architectural change.

### Run the graph inside `api`
- **Pros**: One fewer process type; lower latency.
- **Cons**: Run progress depends on the health of the HTTP process; model calls would run in the process that serves requests.
- **Why not**: An `api` restart would stall every run, and recording intent would no longer be separate from executing it.

### Redis / RabbitMQ / Kafka for dispatch
- **Pros**: Mature queue features; less polling load at scale.
- **Cons**: A new dependency. Enqueue can no longer commit atomically with ledger and audit writes, so an outbox relay becomes necessary.
- **Why not**: It breaks the single-transaction guarantee the side-effect model is built on, with no V1 scale benefit.

### Temporal as the durable substrate; Kubernetes for deployment
- **Pros**: Industrial-grade durability and orchestration.
- **Cons**: Both are explicit PRD non-goals or deferrals for V1; heavy operational footprint.
- **Why not**: Tether builds on LangGraph and must fit a single-host Compose deployment.

## Rationale

With one transactional store, "ledger entry + critical audit + enqueue" is a single atomic commit. That is the foundation for §22 invariants 1, 7, 23 and 25. V1 load is small enough for Postgres to act as both queue and store. Because the processes are stateless, a later move to Kubernetes or another queue is a deployment change, not a redesign.

## Consequences

### Positive
- One backup/restore unit and one consistency domain for all authoritative data.
- `api` and `worker` keep no local state outside Postgres (and the Ollama model volume), so either can be killed at any instruction.
- Workers scale horizontally by replica count.

### Negative
- Postgres is a single point of failure. While it is down nothing progresses, though nothing is lost.
- Run driving and trusted tool code share the worker container. The only process boundary is between `api` and `worker`, and it is not a sandbox.
- Queue polling puts a steady load on the database.

### Risks
- Queue contention at higher scale. Mitigation: the `JobQueue` seam, and splitting worker pools by job kind.

## Failure and Recovery

- If `api` dies, in-flight HTTP requests fail and clients retry. All intent is transactional, so no run state is lost.
- If a `worker` dies, its leases expire and jobs are redelivered under ADR-0004.
- If all workers are down, runs pause without loss. Timers fire late, but expiry and deadlines are also enforced at decision time (ADR-0004, ADR-0006).
- If Postgres is down, nothing progresses and nothing is lost.

## Invariants and Constraints

- §22 invariants 1, 2, 17.
- `api` must never run graph nodes, call models, or execute tools.
- Adding stateful infrastructure in V1 requires a superseding ADR.

## Revisit When

- Concurrency or pending-approval volume grows an order of magnitude beyond P-02/P-03, or queue polling measurably hurts latency budgets.
- A platform-stage availability target requires HA or multi-node Postgres.
- Untrusted tool code needs a sandboxed executor, which would mean separate tool-execution workers.

## References

- PRD: Core Capabilities (tool execution in a separate worker; Postgres-backed queue); Technical Approach / Architecture Notes; What We're NOT Building; Decisions Log (API ↔ worker transport, Tool execution location, Deployment); metrics P-02, P-03; tests C-01–C-03.
- Architecture: §1 (drivers 3, 5), §2 AD-01, §18, §19, §20, §26.
