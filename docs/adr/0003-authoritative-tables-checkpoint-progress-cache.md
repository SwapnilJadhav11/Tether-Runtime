# ADR-0003: Tether tables are authoritative; the LangGraph checkpoint is a replay-safe progress cache

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-10; §24 items 10, 15, 19 (part); review findings F-02, F-03, F-04, F-08

## Context

LangGraph owns graph execution, checkpointing, and interrupts, and Tether must not reimplement them. The checkpoint saver commits on its own connection, so a checkpoint can never share a transaction with Tether's writes. On resume, LangGraph re-runs the interrupted node from its start. After a crash, it re-runs from the last checkpoint. Its default durability mode writes checkpoints asynchronously. If the checkpoint were treated as the record of side effects, a crash at the wrong moment would lose or duplicate world-changing facts.

## Decision

1. **LangGraph decides where the graph is; Tether tables decide what has happened in the world.** No LangGraph API is used for side-effect correctness. Tether never writes the LangGraph schema, and the API never reads checkpoints.
2. The run graph is compiled and invoked with **synchronous durability** (`durability="sync"`). Each step's checkpoint, including the step ordinals and proposals assigned in the model step, is durable before any downstream node creates an authoritative row.
3. Nodes come in two kinds:
   - **Write nodes** create authoritative rows and never interrupt.
   - **Wait nodes** only read authoritative rows and interrupt while the awaited condition is unsatisfied. They perform no writes and no checks.
4. Every node write is **create-or-get by a deterministic key**: `(run, step_ordinal)` for approvals and ledger entries, and a deterministic dedupe key for decision audit events. **Every "get" compares the stored call hash with the current call's hash.** A mismatch is a replay divergence and fails closed: it is audited, and the run fails once any in-flight ledger entry has drained to an outcome or to reconciliation.
5. **Decisions are adopted on replay.** If a decision event already exists for an ordinal, it is used instead of being re-decided. This is safe because policy is re-evaluated against current policy before any side effect (ADR-0009).
6. **Ledger-first.** Once a ledger row exists for a call, no node re-runs policy, hash, or stop checks for that call. A stop request alongside a non-terminal ledger entry means wait, never terminate.
7. **Waiting is dispatch-and-interrupt.**
   - A write node commits the row plus a job.
   - The matching wait node checks its condition *before* calling `interrupt()`, and loops on spurious wakes.
   - The event transaction that changes the awaited row inserts its own wake job (ADR-0004).
   - Resume values and job payloads carry no meaning; the driver always re-reads authoritative rows.
   - No process blocks while a run waits for a tool, an approval, or a reconciliation.
8. Nodes never perform external side effects. Only tool-execution jobs do, and only after `dispatched` has committed (ADR-0007).

## Alternatives Considered

### Make the checkpoint authoritative for side-effect state
- **Pros**: A single source of truth; less code.
- **Cons**: A crash before the checkpoint loses the fact that a side effect was authorized or dispatched.
- **Why not**: It cannot provide the PRD's "no lost runs, no duplicate supported writes" guarantee.

### Share the saver's connection or transaction with Tether writes
- **Pros**: Atomic checkpoint plus Tether rows.
- **Cons**: Couples Tether to LangGraph internals, and still does not cover the external call.
- **Why not**: It is fragile across LangGraph upgrades and solves only part of the problem.

### Default (async) durability
- **Pros**: Lower per-step latency.
- **Cons**: After a crash, step ordinals can be reassigned, so one ordinal could refer to two different calls (F-03).
- **Why not**: It defeats the deterministic keys that replay safety relies on.

### Combined nodes (check + write + interrupt in one node)
- **Pros**: Fewer nodes.
- **Cons**: On resume the node re-runs its checks before reading the ledger, which can hide in-flight or applied outcomes (F-02).
- **Why not**: It is unsafe under LangGraph's re-run-from-start resume semantics.

### Blocking waits, or meaningful resume payloads
- **Pros**: Simpler control flow.
- **Cons**: Blocking holds compute during approval waits of up to days (P-03). Payload-carried outcomes are corrupted by duplicate, early, or lost wakes.
- **Why not**: It breaks P-03 and crash safety.

## Rationale

This design treats replay as normal, not exceptional. Any node can run again at any time and reach the same authoritative outcome, or fail closed if the replay diverged. LangGraph keeps every concern it owns. Tether relies only on its documented durability and interrupt semantics.

## Consequences

### Positive
- A crash at any instruction is replay-safe, and a divergent replay can never attach one call's outcome to another call.
- Waits hold no compute.
- LangGraph upgrades do not change the correctness model, provided the durability and interrupt semantics hold.

### Negative
- Synchronous durability adds one checkpoint write of latency per step.
- Every new node must follow the write/wait discipline, which adds review and test burden.
- A model call can repeat after a crash before its checkpoint. Both calls are recorded, so cost stays truthful.
- A replay divergence fails the run rather than repairing it.

### Risks
- LangGraph could change its durability or interrupt semantics. Mitigation: pin the version and keep replay contract tests.

## Failure and Recovery

- **Crash after a Tether transaction but before the next checkpoint:** the node replays, create-or-get returns the existing hash-checked rows, and the outcome is the same.
- **Crash during the model step before its checkpoint:** the provider is called again; proposals are inert until checkpointed.
- **Divergence:** a `REPLAY_DIVERGENCE` audit event is written, and the run fails after any in-flight entry drains.

## Invariants and Constraints

- §22 invariants 1, 2, 3, 21, 22, 24, 27.

## Revisit When

- LangGraph changes durability or interrupt semantics, or offers transactional checkpoint hooks.
- Repairing divergent runs (instead of failing them) becomes a requirement.
- Tether adopts a different durable-execution substrate.

## References

- PRD: Tether ↔ LangGraph boundary; Core Capabilities (durable runs); Success Metrics (run durability, duplicate-safe side effects, reconstructability); tests C-01–C-07, T-12.
- Architecture: §3, §9.1, §9.2, §9.3, §13 AD-10, §13.2, §13.3, §21 Q1 and Q7, §26, §27 F-02/F-03/F-04/F-08.
