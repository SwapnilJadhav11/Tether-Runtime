# PostgreSQL backup, restore, outage and failover

> **Status:** required V1 documentation (direction record TDR-021); recovery objectives are **unresolved** (`TBD (owner, U-007)`)
> **Applies to:** the single Postgres instance that ADR-0001 makes Tether's only stateful dependency
> **Baseline:** commit `ec35254`

Tether does not implement its own database high availability. This document states what must be protected, how the system behaves while the database is unavailable, how to back up and restore, how to validate a restore, what posture is recommended, and what a restore does to leases, dispatch records, approvals, reconciliation and audit continuity.

## 0. Architectural position (owner decision OD-4, 2026-11-09)

- **Every side-effect guarantee assumes committed authoritative history is never rolled backward.** The ledger, approvals, audit chain and jobs are the record of what Tether authorized and dispatched; the guarantees in the guarantee card hold only while that record is intact.
- **Normal production HA/failover must preserve committed history.** Synchronous replication or a managed service with failover that loses no committed transaction keeps the guarantees; a failover is then equivalent to a process restart (§2).
- **A restore that loses committed Tether state is a recovery discontinuity.** Backups and point-in-time recovery are disaster-recovery mechanisms. V1 does not claim that an arbitrary PITR preserves exactly-once or any other side-effect guarantee.
- **External effects in the lost interval are unknowable from the database alone.** Writes dispatched after the restore point may have happened; the restored database has no record of them. No mechanism claims to reconstruct them.
- **After a data-loss restore, side-effect dispatch enters an operator-controlled recovery posture before normal dispatch resumes.** Workers are not started until an operator has reviewed the entries listed in §6.2 and decided, with the target's help where possible, what to do with each. The procedure is defined in §4 and §6; its tooling and the concrete recovery objectives are decided in W8.1 (`TBD (owner, U-007)`).
- **Preferred production topology:** managed Postgres with HA/failover intended to avoid committed-data loss (§7), with backups/PITR kept for disaster recovery.

## 1. Authoritative data and what must be backed up

Everything authoritative lives in one Postgres database, `tether`, in two schemas:

| Schema | Owner | Contents | Must be backed up |
|---|---|---|---|
| `tether` | Tether migrations | `workspaces`, `issuers`, `api_keys`, `agents`, `policy_versions`, `runs`, `approvals`, `ledger_entries`, `audit_heads`, `audit_events`, `jobs`, `model_calls`, `access_events` | **Yes.** This is the reconstruction record (ADR-0015) and the side-effect record (ADR-0003). |
| `langgraph` | LangGraph checkpoint saver | checkpoints and writes | **Yes, in the same snapshot.** The checkpoint is a progress cache; restoring it from a different point in time than the `tether` schema would let a run resume from a position inconsistent with its ledger. |

Not in the database, and also required to recover: the audit and call-binding HMAC keys and their key IDs (through `SecretsProvider`, env in V1), issuer public keys and API-key hashes (bootstrap YAML, in version control), provider API keys. **Without the audit keys of the backed-up period, restored chains cannot be verified; without the binding keys of non-terminal runs, those runs cannot proceed (ADR-0010).**

The demo target's `demo_target` database is demo data; it is not Tether state and is not covered here.

## 2. Behaviour during database unavailability

- `api`: requests fail (readiness `/readyz` reports not ready); clients retry. No intent is lost because every submission, decision, reconciliation and cancellation is a single transaction that either committed or did not.
- `worker`: leasing, heartbeating (once implemented) and every transaction fail; nothing progresses. Jobs whose leases expire during the outage are redelivered afterwards under the per-kind rules of ADR-0004.
- Side effects: a tool call that was in flight when the database went down cannot record its outcome; when the database returns, its ledger entry is still `dispatched`. Idempotent writes are re-sent with the same key within their budget; non-idempotent writes and exhausted budgets become `indeterminate` and wait for verification (ADR-0019, once implemented) or human reconciliation. No write is lost or silently duplicated.
- Timers (approval expiry, run deadline) fire late but are also enforced at decision time against the database clock, so no decision is made on an expired approval or past a deadline.

Summary: **while Postgres is down, nothing progresses and nothing is lost.** The visible cost is latency and, for writes caught mid-flight, reconciliation work.

## 3. Backup procedure

Recommended for V1 (single instance, Compose): continuous archiving plus periodic base backups, so that a restore can reach a point in time.

1. Enable WAL archiving on the Postgres instance (`archive_mode = on`, `archive_command` to durable storage outside the host), or use a managed Postgres service that provides point-in-time recovery.
2. Take a base backup at least daily (`pg_basebackup` against the instance, or the managed service's snapshot). For small deployments, a logical dump of the whole `tether` database (`pg_dump --format=custom tether`) is an acceptable minimum; it captures both schemas in one consistent snapshot.
3. Store the active and retired audit and binding keys, and the bootstrap YAML of the same period, alongside the backup metadata (never in the database, never in plain text in the repository).
4. Record for each backup: timestamp, Postgres version, Tether migration revision (`alembic` head), LangGraph pins, key IDs in use.

Do not back up only the `tether` schema or only the `langgraph` schema.

## 4. Restore procedure

1. Stop `api` and `worker`. Do not restart them until step 7.
2. Restore the base backup and replay WAL to the chosen point in time, or restore the logical dump into an empty `tether` database created by `deploy/compose/postgres/init.sh` (roles `tether_migrator`, `tether_app`, schemas, grants).
3. Run `tether migrate`. It is idempotent; if the backup predates the current migration head it applies the missing revisions. The application that starts in step 7 must be the version that matches the resulting schema: never start processes older than the schema, and never downgrade the schema to fit older processes once version-2 audit events exist (`production-configuration.md` §6). To return to an older application, restore a backup taken under that version together with it, and accept everything committed after that backup as a recovery discontinuity (§0).
4. Run `tether bootstrap apply` with the bootstrap file of the current deployment, so workspaces, issuers, API-key hashes, agents and the active policy version match the processes that will start.
5. Make the audit and binding keys of the restored period available through the environment, including retired key IDs referenced by restored runs.
6. **Recovery posture (if any committed state was lost).** Before any worker starts, an operator lists the ledger entries that could dispatch or re-send on resume (`pending` entries, and `dispatched` entries of idempotent tools) and resolves each one deliberately, using the target's own records where they exist: confirm with the target and reconcile, or hold the run by reconciling the entry as `indeterminate`/`abandon`, or accept the re-send for an idempotent write whose target retains keys. Record the restore point, the backup identifier and each decision in the operations log. *The tooling for this step (a "recovery mode" that lists and holds such entries instead of relying on manual SQL) is defined in W8.1; until then this is a manual, documented procedure.*
7. Start `worker` first, then `api`. Observe §6 effects.

## 5. Recovery validation

After a restore, before declaring recovery complete:

- `/readyz` on `api` returns ready; the worker heartbeat file is fresh.
- Run inspection on a sample of runs from before the restore point returns a timeline whose chain-valid flag is true. *(`tether audit verify --workspace <ws>` is the planned command, D-10; until it exists, inspection's chain flag is the check.)*
- Count runs in `awaiting_reconciliation` and ledger entries in `indeterminate`; each must be visible in inspection and resolvable (§6.3).
- Count jobs in `leased` with `lease_expires_at` in the past: they must be re-leased within one poll interval.
- Confirm `policy_versions` has exactly one active row per workspace and that `api`/`worker` started (they refuse to start on a version mismatch, ADR-0009).

A restore drill (backup, restore into a scratch instance, run this list) should be performed and recorded before any non-demo use. *Frequency: `TBD (owner, U-007)`.*

## 6. Effect of a restore or failover on runtime records

Restoring to point **T** discards everything that committed after T. Because all records are in one database and were written transactionally, the restored state is internally consistent; what it is **not** consistent with is the outside world, which kept the effects of writes dispatched after T.

### 6.1 Leases and jobs
Jobs leased after T reappear as `ready` or as `leased` with expired leases; they are re-leased. Wake jobs inserted after T are gone, but so are the state changes that inserted them, so no run is stranded (every restored state-changing transaction carried its own wake).

### 6.2 Dispatch records and the ledger
- An entry `dispatched` at T whose write completed after T: after restore it is still `dispatched`. Idempotent write → re-sent with the same key; the target returns the original result (safe). Non-idempotent write, or budget exhausted → `indeterminate` → verification/reconciliation. **Correct, with reconciliation cost.**
- An entry `pending` at T whose TX6 committed after T: if workers simply resumed, the entry would be dispatched again. For a non-idempotent write this would be a **second send of a write that may already have happened**, because the restored ledger has no record of the first dispatch. This is why §0 forbids blind resumption: such entries are resolved by the operator in the recovery posture (§4 step 6) before any worker starts. Tether cannot determine from its own database whether the lost-interval send occurred; only the target can.
- Entries resolved after T revert to their pre-T state; a human reconciliation made after T must be repeated.

### 6.3 Approvals
Decisions made after T are lost; those approvals revert to `pending` (or `expired`, if their expiry has since passed, enforced at decision time). Approvers must decide again. Expiry timers are re-materialised by the restored jobs.

### 6.4 Reconciliation
Reconciliations after T are lost and must be redone; the ledger entries are `indeterminate` again and the runs show `awaiting_reconciliation`.

### 6.5 Audit continuity
Per-run chains are restored intact up to T; events after T are gone together with the state they described, so the chain still verifies. No gap is detectable from the chain itself (tail truncation is a documented non-detection), so the **restore itself must be recorded** in operations logs with T and the backup identifier. Access events after T are lost (acceptable by design). Restored events keep the `mac_version` they were written with: version 1 (before W2.4, timestamp not covered by the MAC) or version 2 (timestamp covered); a restored chain may mix both, and it verifies only with an application version that understands `mac_version`.

### 6.6 Checkpoints
Restored with the same snapshot, so a run resumes from a checkpoint no newer than its ledger. A checkpoint older than the ledger is safe: nodes re-run and create-or-get finds the existing rows.

### 6.7 Failover to a replica
A synchronous replica promoted after the primary fails loses nothing committed; the effects are those of a worker/api restart (§2). An asynchronous replica loses the un-replicated tail and behaves like a restore to point T (§6.1–6.6); its lag bounds the exposure.

## 7. Recommended deployment posture

| Posture | Fits | Data loss on failure | Notes |
|---|---|---|---|
| Single instance, daily dump (Compose default) | development, demos | up to a day | Not for non-demo use; any restore is a recovery discontinuity (§0) |
| Single instance, WAL archiving + base backups | small self-hosted use | seconds to minutes of WAL | Disaster recovery only; every restore to an earlier point requires the recovery posture (§4 step 6) |
| **Managed Postgres with HA/failover that preserves committed transactions, plus PITR for disaster recovery** | production | none on failover; PITR for operator error, with the recovery posture | **Preferred (OD-4)**; Tether needs only a connection string change |
| Self-managed HA (e.g. Patroni, synchronous replication) | production without a managed service | none with synchronous replication | Operationally heavier |

Tether itself needs: one connection URL per role (`TETHER_DATABASE_URL` for `tether_app`, `TETHER_MIGRATOR_DATABASE_URL` for migrations), `clock_timestamp()` semantics (any Postgres 16), and that `api`/`worker` are restarted or reconnect after failover.

## 8. Decision gates (`TBD (owner, U-007)`)

| Gate | To decide |
|---|---|
| Recovery point objective | acceptable data loss (seconds / minutes / a day) |
| Recovery time objective | acceptable downtime for a restore |
| Supported topology for V1 claims | which row of §7 V1 documentation and tests assume |
| Restore drill frequency | and who records it |
| Recovery-posture tooling | whether W8.1 builds a "recovery mode" that lists and holds dispatchable entries after a restore, or the manual procedure in §4 step 6 stands for V1 |

Until these are decided, V1 claims only (OD-4): *nothing is lost while Postgres is down; HA that preserves committed transactions preserves every guarantee; a restore that loses committed state is a recovery discontinuity after which side-effect dispatch resumes only through an operator-controlled recovery posture, because external effects in the lost interval cannot be reconstructed from the database.*
