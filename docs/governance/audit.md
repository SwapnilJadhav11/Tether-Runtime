# Audit

How Tether records every consequential step of a run, how that record is protected against
modification, and what its protection does not cover. This page is for security reviewers, auditors
and operators. The command reference for `tether audit verify` is in the
[CLI reference](../reference/cli.md); keys are covered in
[Secrets and data handling](../security/secrets-and-data-handling.md).

## The record is written with the facts

Each run has its own ordered sequence of **audit events** in the table `tether.audit_events`. A
critical event is appended **in the same database transaction** as the state change it describes
([ADR-0015](../adr/0015-per-run-hmac-audit-and-authoritative-reconstruction.md)). If the append
fails, the transaction rolls back and the state change does not happen. There is no buffer and no
asynchronous writer: the audit record is exactly as durable as the facts it describes.

Because the dispatch record (`DISPATCHED`) commits before a tool is called, no side effect sent
through Tether's dispatch path happens without its event in the chain. The application database
role may only insert audit events; it cannot update or delete them. Only audit and access events are
insert-only, though: anyone holding the application role's database credential (including tool code
running in the worker) can update approval, ledger, run, job and audit-head rows.

## What is audited

| Group | Event | Written when |
|---|---|---|
| Run | `RUN_SUBMITTED` | The API accepts a run; actor is the requester |
| | `RUN_FINISHED` | The run reaches a terminal status; carries the status and summary |
| | `DEAD_LETTERED` | A run's `advance_run` job runs out of deliveries |
| | `REPLAY_DIVERGENCE` | A replay finds persisted state recorded for a different call; the run fails closed |
| Governance | `POLICY_DECISION` | A proposal is decided; carries the decision, reasons, call hash, pinned tool version and policy version |
| | `CALL_INVALID` | A proposal names a tool that is not offered, or its arguments do not validate |
| | `CALL_SKIPPED` | A later call in the same model turn is skipped because an earlier one did not apply, unless the run is ending because of a stop or an exhausted budget |
| Approvals | `APPROVAL_REQUESTED` | An approval is created; carries the call hash and masked arguments |
| | `APPROVAL_DECIDED` | An approver approves or rejects; actor is the approver |
| | `APPROVAL_EXPIRED` | The expiry timer records a lapsed approval |
| | `APPROVAL_CLOSED` | A stop closes a pending approval |
| Execution | `EXECUTION_AUTHORIZED` | The ledger entry is created; carries the call hash, idempotency key and policy version |
| | `DISPATCHED`, `REDISPATCHED` | A first send or a re-send is committed, before the call |
| | `TOOL_OUTCOME` | A ledger entry reaches its outcome state; carries the state, outcome class and reason |
| Verification | `VERIFICATION_ATTEMPTED` | A probe's gate commits |
| | `VERIFICATION_INCONCLUSIVE` | A probe gives no definitive answer |
| | `VERIFIED` | A probe resolves the entry |
| | `VERIFICATION_EXHAUSTED` | Automatic verification ends without resolving; carries the reason |
| | `VERIFICATION_SKIPPED` | A stop prevents a probe |
| | `VERIFICATION_LATE` | A probe answers after the entry was already resolved; evidence only |
| Reconciliation and stops | `RECONCILED` | A person resolves an `indeterminate` entry; carries the outcome and justification |
| | `CANCEL_REQUESTED` | A cancellation is recorded; actor is the person who cancelled |
| | `DEADLINE_EXCEEDED` | The deadline timer records that the run's deadline passed |

Probe events (`VERIFICATION_ATTEMPTED`, `VERIFICATION_INCONCLUSIVE`, `VERIFIED`,
`VERIFICATION_LATE`) carry the actor `system:verification`. `VERIFICATION_EXHAUSTED` and
`VERIFICATION_SKIPPED` carry no actor. Every event records the tool it concerns, where there is
one.

Not audited: transitions between waiting statuses (`running`, `awaiting_approval`, `awaiting_tool`,
`awaiting_reconciliation`), which no decision depends on; and a refusal at the re-check just before
a ledger entry would be created, which leaves only the model's typed result and the absence of
`EXECUTION_AUTHORIZED`. Model calls are recorded in `tether.model_calls`, not in the chain.

## Payloads are masked

An event's payload is stored as `payload_masked`, and only already-masked data may be appended.
Arguments, precondition values, tool output and verification evidence pass through the masking
function first, so a field a tool marks `Sensitive` never appears in an audit event. Some events carry
no arguments at all: `CALL_SKIPPED` records identifiers and the cause only, and a probe exception
is recorded by its type, not its message. Some content is stored as given: free text supplied by
people (an approval reason, a reconciliation justification); the requester's subject and roles in
`RUN_SUBMITTED`; and, in `RUN_FINISHED`, the model's final text or, for a failed run, a summary that
can include a model provider's error text. See
[Secrets and data handling](../security/secrets-and-data-handling.md).

## The per-run HMAC chain

Each event carries a MAC: HMAC-SHA-256, under an audit key, over the RFC 8785 canonical JSON of the
event's fields, including the previous event's MAC. The events of one run therefore form a chain:
changing or reordering an event, or removing one from the middle, breaks the chain at that point.
Removing the last events is caught only through the head check.

| Field in the MAC record | MAC version 1 | MAC version 2 |
|---|---|---|
| `workspace_id`, `run_id`, `seq` | yes | yes |
| `dedupe_key`, `event_type`, `actor`, `tool` | yes | yes |
| `payload` (masked) | yes | yes |
| `policy_version` | yes | yes |
| `prev_mac` | yes | yes |
| `mac_version` | no | yes |
| `created_at` | no | yes, as UTC ISO 8601 with six fractional digits and `Z` |

Every new event is version 2. Its `created_at` is read from the database clock under the run's head
lock, stored exactly, and covered by the MAC. Version 1 is the format before the MAC-version
migration; those events stay valid as written, and **their timestamps are not covered**. One run's
chain may mix both versions. Each event states its own version, and because the version is inside a
version 2 MAC, rewriting it to drop the timestamp from the check breaks the event. Any other version
fails verification.

**The key.** Audit keys come from the `TETHER_AUDIT_KEY_<ID>` family, with the active one named by
`TETHER_AUDIT_ACTIVE_KEY_ID`. They are never stored in the database and are separate from the
call-binding keys. Each run is pinned to the key that was active when its first event was written, so
rotating keys never re-keys an existing chain. Retired keys must stay available for as long as their
chains need verifying. Without its key, a run can neither append events nor be verified.

**The head.** The table `tether.audit_heads` holds one row per run: the last sequence number, the last
MAC and the pinned key. Every append locks this row, so a run's appends are serialised and sequence
numbers are contiguous. Runs never contend with each other.

**Dedupe keys.** Every event has a dedupe key, unique within its run, and an append is create-or-get:
if an event with that key exists, it is returned unchanged. Decision events use deterministic keys
(`decision:<ordinal>`, `authorized:<ordinal>`, `approval_requested:<ordinal>`), so a replayed step
adopts the recorded decision instead of deciding twice; adoption also checks the recorded call hash,
and a mismatch is a `REPLAY_DIVERGENCE`. Events that can genuinely repeat carry the occurrence in
their key, such as the attempt number in `dispatched:<entry>:<attempt>`.

## Verifying a chain

`tether audit verify --workspace <workspace> --run <run-id>` recomputes one run's chain from the
database, in a single snapshot, and also checks the persisted head. It needs the application role's
`TETHER_DATABASE_URL` and the audit key family, and it changes nothing. Illustrative output for an
intact run:

```text
VALID
workspace: ws-example
run: <run-id>
events: 12
last_seq: 12
head: seq=12 consistent
mac_versions: 2
```

| Exit code | Meaning |
|---|---|
| 0 | `VALID`: the chain recomputes and the head agrees |
| 1 | `INVALID`, with a reason: `sequence_mismatch`, `prev_mac_mismatch`, `unsupported_mac_version`, `mac_mismatch` or `head_mismatch`, and, except for `head_mismatch`, the first failing sequence number |
| 2 | Usage or configuration error; nothing was verified |
| 3 | The database or an audit key was unavailable; nothing can be said about the chain |
| 4 | `NOT_FOUND`: no such run in that workspace (another workspace's run is also `NOT_FOUND`) |

The output carries identifiers, counts and codes only: never payloads, MACs, keys or connection
details.

Run inspection through the API (the requester's view, the approver's run evidence and the ops run
view) reports `audit_chain_valid`. It recomputes the chain only and does **not** check the persisted
head, so removing a run's last events is invisible there; only `tether audit verify` compares the
head.

### What verification detects

- A change to any MAC-covered field of any event, including a masked payload or, for version 2, the
  timestamp.
- Reordering events, moving one to a gap, or swapping two and repairing their links.
- A changed, unknown or downgraded `mac_version`.
- Events deleted from a chain while its head row is left alone (`head_mismatch`).

### What it does not detect

- Deleting a chain's tail **and** rewriting its head row to match, or deleting a whole run.
- A change to the timestamp of a version 1 event.
- Anything across runs: there is no global chain and no cross-run order.
- Anything done by someone who holds the audit key and can write to the database: they can
  recompute a valid chain.

The audit log is tamper-evident within these limits. It is not WORM storage and not a compliance
product.

## Reconstructing a run

The audit chain, read with the ledger, approvals and model-call rows, is the authoritative account of
a run. Run inspection is assembled from these Tether tables only; it never reads traces or LangGraph
checkpoints. For a run in which the model first reads a service's health and then restarts it after
approval, the chain is:

| `seq` | Event | What it shows |
|---|---|---|
| 1 | `RUN_SUBMITTED` | Who submitted it, with which agent |
| 2 | `POLICY_DECISION` | `get_service_health` allowed, with its call hash and policy version |
| 3 | `EXECUTION_AUTHORIZED` | Ledger entry for the read created, with its idempotency key |
| 4 | `DISPATCHED` | First send of the read committed, before the call |
| 5 | `TOOL_OUTCOME` | The read `completed`, `applied` |
| 6 | `POLICY_DECISION` | `restart_service` requires approval, with its call hash |
| 7 | `APPROVAL_REQUESTED` | Approval created for that call hash, with masked arguments |
| 8 | `APPROVAL_DECIDED` | Approved, by the approver named as actor |
| 9 | `EXECUTION_AUTHORIZED` | Re-check passed; ledger entry for the restart created with the same call hash |
| 10 | `DISPATCHED` | First send of the restart committed, before the call |
| 11 | `TOOL_OUTCOME` | The restart `completed`, `applied` |
| 12 | `RUN_FINISHED` | `succeeded`, with the summary |

From this a reviewer can answer: who asked, what the model proposed, which policy version decided it,
who approved exactly which call, that the executed call is the approved one (equal call hashes), when
each send was committed relative to the call, and what came back. After a crash, the same sequence
shows where the run stopped: a `DISPATCHED` with no `TOOL_OUTCOME` is a send whose outcome was not
yet recorded. The chaos suite checks after every scenario that each ledger entry has its decision and
authorization events and that every dispatch has an outcome or is still in flight; see
[Validation](../validation.md).

## Access events

Reads of a run through the approver or ops audiences also leave a trace, separately from the chain:
the approver's run evidence view and the ops run view each write one **access event** (`run.inspect`)
to `tether.access_events`, recording who read which run, through which audience, with which roles.

Access events are operational access logs, not part of the run's record. They are queued during the
request and written asynchronously, in batches, and they are best effort: an event lost to a full buffer, a failed write or a crash is
counted in the metric `tether.access_events.dropped`, and a clean API shutdown flushes what is
buffered. They are not chained. A refused read writes none, and the approval list and detail routes
write none. The application role can only insert them.

## Audit and traces

Every audit event stores the trace ID of the operation that wrote it, so an auditor can jump from an
event to its trace. The trace ID is outside the MAC: it is correlation, not evidence. Traces are
best effort and may have gaps after a process is killed; the audit chain does not. See
[Observability](../operations/observability.md).

## Related

- [Guarantees](../guarantees.md)
- [CLI reference](../reference/cli.md)
- [Secrets and data handling](../security/secrets-and-data-handling.md)
- [Data model](../architecture/data-model.md)
- [Approvals](approvals.md)
