# ADR-0015: Per-run HMAC audit chain with write-ahead critical events; Postgres is the reconstruction record

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-12, AD-13; §13.3 audit dedupe classes; §24 items 12, 13; review findings F-08, D-05, D-08

## Context

The PRD requires:
- a per-run, HMAC-chained audit log keyed through `SecretsProvider`, with no global chain;
- critical events (policy decision, approval, ledger transition, dispatch) persisted before the side effect, failing closed otherwise;
- a full timeline reconstructable from authoritative Postgres data, with traces treated as best-effort.

Replays must not duplicate decision events or drop genuine repeats, and killed processes lose buffered spans.

## Decision

1. **Per-run chain.** Each run has its own ordered audit chain. Each event's MAC is HMAC-SHA-256 over its canonical fields:
   - workspace, run, sequence;
   - type, actor, tool;
   - masked payload;
   - policy version, time;
   - the previous MAC.
   The key comes from `SecretsProvider`, is never stored in the database, and carries a key ID for rotation. A CLI verifies chains.
2. **Write-ahead.** Critical events are appended **inside the same transaction** as the state change they authorize or describe, under a per-run head-row lock. If the append fails, the transaction aborts, so no side effect follows.
3. **Dedupe classes.**
   - Decision events use deterministic keys and are create-or-get; they are adopted on replay.
   - Observation events (model attempts, fallback, dispatch and re-dispatch attempts, late results) use keys unique to each occurrence.
4. Audit events are **append-only** at the database-grant level.
5. **Access events** (inspections, audit queries) are separate, asynchronous, and unchained. Losing some on a crash is acceptable.
6. **Reconstruction.** Run inspection and audit query are assembled from Tether tables only: runs, audit events, ledger, approvals, and model-call records (including failed attempts). They never read traces. The API never reads checkpoints; checkpoints stay authoritative for *resuming* runs.
7. **Telemetry is best-effort.**
   - Trace context is created at submission and persisted on the run and on every job, so every segment of a run shares one trace ID across restarts.
   - Audit events store the trace ID.
   - Logs are JSON with run and trace IDs and a redaction processor.
8. **Stated limits.** V1 does not detect whole-run deletion or tail truncation, and has no cross-run chain.

## Alternatives Considered

### Global chain
- **Pros**: Detects cross-run deletion.
- **Cons**: Serialises every run's appends, threatening the P-01 budget.
- **Why not**: The PRD chooses per-run chains.

### Unkeyed hash chain
- **Pros**: No key management.
- **Cons**: Anyone with database write access can recompute it.
- **Why not**: Provides no tamper evidence against the stated threat.

### Asynchronous or buffered audit
- **Pros**: Lower latency.
- **Cons**: Events can be lost on a crash, and write-ahead is impossible.
- **Why not**: The PRD requires fail-closed write-ahead.

### WORM storage or an external anchor now
- **Pros**: Detects truncation.
- **Cons**: A PRD non-goal for V1.
- **Why not**: Left as a future hook.

### Traces as the record; checkpoint-derived inspection
- **Pros**: Reuses existing data.
- **Cons**: Spans are lost when a process is killed, so T-12 would fail in chaos tests. Checkpoints contain unmasked arguments and LangGraph internals.
- **Why not**: Neither is authoritative or safe to serve.

## Rationale

Keeping the audit and the state it describes in one transaction makes the record exactly as durable as the facts. Per-run chains give tamper evidence without contention across runs. Telemetry stays useful without carrying correctness.

## Consequences

### Positive
- T-12 and T-13 are provable after every test, including chaos tests.
- The only contention is within a single run.

### Negative
- Synchronous audit sits on the hot path and must fit the P-01 budget.
- The head lock serialises appends within a run.
- Truncation is not detected.
- Traces may have gaps after a process is killed.

### Risks
- Losing the audit key makes chains unverifiable. Mitigation: key-ID rotation, and keeping retired keys available for verification.

## Failure and Recovery

- If an append fails, the transaction rolls back and execution fails closed.
- If the audit key is unavailable, no critical write can happen, so nothing progresses.
- Lost spans degrade diagnostics only.

## Invariants and Constraints

- §22 invariants 7, 14, 17, 27.

## Revisit When

- A compliance-grade or WORM requirement appears.
- An external anchor is added.
- The PRD's data-retention question is answered.
- Cross-run audit queries outgrow per-workspace indexes.

## References

- PRD: Audit chain guarantee; Critical audit persistence; Reconstruction source of truth; Authorization (access events); Core Capabilities (audit, observability); What We're NOT Building (WORM, global chain); tests T-12, T-13; P-01.
- Architecture: §13.1, §13.3, §16 AD-12, AD-13, §18, §20, §21 Q6, §24 items 12, 13, §27 F-08, D-05, D-08.
