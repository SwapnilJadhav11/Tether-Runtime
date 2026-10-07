# ADR-0005: Run-driver mutual exclusion: session advisory lock plus per-run fencing token

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-08 (per-run mutex); §9.3 step 1; §24 items 8 (part), 19 (part), 21 (A-02); review findings F-04, D-09
**Resolves**: A-02

## Context

At-least-once delivery and non-coalesced wake jobs (ADR-0004) mean two workers can hold `advance_run` jobs for the same run at once. Two concurrent drivers would fork the checkpoint thread and make different, nondeterministic model calls from the same step ordinals. A per-job lease token cannot serialise them, because duplicate jobs have different leases.

The per-run mutex is a session-level advisory lock held on a dedicated connection. If that connection drops, Postgres releases the lock, but the original driver's code may keep running until its heartbeat notices. That is a **zombie driver**. Uniqueness constraints stop duplicate approvals and ledger rows, but they do not stop two other problems:
- **ABA status writes.** A zombie can "finalize `succeeded`" while the live driver happens to be in `running` again.
- **Decision races.** A zombie can race in a decision built from a different model response.

## Decision

1. **Lease, then lock.**
   - After leasing an `advance_run` job, the worker takes a session-level advisory lock on the run, on a dedicated connection held for the whole invocation.
   - If the lock is contended, the job goes back to ready after a short delay *without consuming an attempt*.
   - The advisory lock is the per-run mutex. A lease-query concurrency key is never relied on for exclusion.
2. **Fencing token.** Right after acquiring the lock, the driver increments a monotonic per-run **driver generation** on the run row and keeps the new value as its fencing token. The TX12 dead-letter function for `advance_run` does the same, under the lock.
3. **Every run-driver authoritative write asserts the token** under the run-row lock it already takes. This covers TX2, TX3, TX5, TX11, the TX12 `advance_run` path, and every other decision event a graph node writes (for example, the skip events in ADR-0012).
   - On a mismatch, the transaction rolls back with no write, and the invocation aborts.
   - The fenced invocation never completes its job. It releases the job for redelivery without consuming an attempt (or lets the lease expire). A wake must never be dropped, and a duplicate wake is a no-op.
   - The event is logged and counted as a metric.
4. **Model-call attempt records** are always committed, so cost stays truthful. The same transaction reads the generation. If the token is stale, the model step fails after recording the attempt, so a fenced driver's proposals never complete a node and never reach a checkpoint through a successful step.
5. **Heartbeat or lock-connection failure aborts the invocation.** No new provider attempt or write node starts.
6. **Residual window.** The lock could be lost after a node's last fenced check but before LangGraph writes that node's checkpoint. In that case a stale checkpoint can be written; Tether cannot fence it because LangGraph owns that write. Any resulting fork is caught by hash-checked create-or-get and fails closed (ADR-0003). Safety still holds, because no side effect is reachable without a fenced TX5 followed by a TX6 that is conditional on ledger state.
7. API-side transactions (TX4, TX8, TX9) and tool-execution transactions (TX6, TX7) are not driver writes and are not generation-fenced. They are serialised by run-row locks and ledger conditional updates.

## Alternatives Considered

### Lease-token predicate on TX5/TX11 (the §24 recommendation, taken literally)
- **Pros**: Reuses an existing column.
- **Cons**: A per-job token does not fence a driver holding a *different* duplicate wake job. Making it work would require coalescing, which F-01 removed.
- **Why not**: The driver generation is the per-run equivalent of a lease token. It is adopted as the correct realisation of the recommendation, and extended to every driver write rather than only TX5/TX11.

### Advisory lock alone, relying on heartbeat abort
- **Pros**: No extra column.
- **Cons**: Between connection loss and detection, a zombie can commit stale writes, including a wrong terminal status.
- **Why not**: The window is real and unbounded by design.

### Check advisory-lock ownership by backend pid inside each transaction
- **Pros**: No schema change.
- **Cons**: Couples correctness to backend pid lifecycle and catalog queries; harder to test.
- **Why not**: A token is the standard fencing pattern, and easier to reason about and test.

### Hold a row lock on the run for the whole invocation
- **Pros**: Simple exclusion.
- **Cons**: Holds a transaction open across model calls that can take minutes. Cancellation, approval, and reconciliation would block behind it, and vacuum suffers.
- **Why not**: Cancellation must never wait on a model call.

### Wrap the LangGraph saver to fence checkpoint writes
- **Pros**: Closes the residual window.
- **Cons**: Couples Tether to LangGraph internals.
- **Why not**: Rejected in ADR-0003. The residual window already fails closed.

## Rationale

This is the classic fencing-token pattern. The check piggybacks on row locks that TX2, TX3, TX5 and TX11 already take, and needs no long transactions. The worst a zombie can do is waste a model call (which is recorded) or, in a narrow window, turn a liveness problem into a fail-closed run. It can never cause an unsafe side effect.

## Consequences

### Positive
- At most one driver can change a run's authoritative state.
- Zombies are deterministic to test: kill the lock connection mid-invocation.
- Lock contention never consumes delivery attempts.

### Negative
- One extra small UPDATE per invocation.
- Every new driver write must include the fence, which adds review and test burden.
- The residual checkpoint-fork window can fail a run. This costs liveness, not safety.

### Risks
- A driver write that forgets the fence. Mitigation: route all driver writes through a helper that requires the token, plus a chaos test that kills the lock connection.

## Failure and Recovery

- **Lock connection lost:** the zombie aborts at its next heartbeat or fenced write, and the live driver continues from the checkpoint. If a stale checkpoint was written, the next replay hits `REPLAY_DIVERGENCE` and the run fails after any in-flight entry drains.
- **Contention:** the job is requeued without consuming an attempt. A stuck lock holder resolves when its session dies.

## Invariants and Constraints

- §22 invariants 3, 8, 10.
- New invariant **30** (added to architecture §22). Every run-driver authoritative write asserts the run's current fencing token, and a stale token aborts the write.

## Revisit When

- LangGraph supports conditional or fenced checkpoint writes.
- Run driving moves to a separate service or an external coordinator.

## References

- PRD: Success Metrics (run durability, duplicate-safe side effects); Run lifecycle; tests C-01–C-03, C-07.
- Architecture: §9.3, §10 AD-07, §11 AD-08 (per-run serialization), §13.1 (TX2, TX3, TX5, TX11, TX12), §24 items 8, 19, 21, §25 (advisory-lock connection management), §27 F-04, D-09, A-02.
