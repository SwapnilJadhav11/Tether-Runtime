# Guarantees

What Tether guarantees about consequential actions, in plain language, and where each guarantee
stops. This page is for engineers and security reviewers deciding how far to rely on Tether. The
authoritative statement of every guarantee, with the code that implements it and the tests that
prove it, is the [guarantee card](product/tether-v1-guarantees.md). If this page and the card ever
differ, the card is right.

The page has three parts, and they must not be read as one list:

1. **Proven guarantees.** Each is implemented, covered by automated tests that run in CI, and
   accepted as a guarantee.
2. **Implemented and tested, wording under review.** The behaviour exists and passes automated
   tests, but it is not yet a guarantee. Do not rely on it as one.
3. **What Tether does not guarantee.**

Terms such as *ledger entry*, *call hash* and *stop* are defined in the [glossary](glossary.md).
[Validation](validation.md) explains how the claims were checked and the limits of that evidence.

## One assumption under every guarantee

Every proven guarantee assumes that committed history in PostgreSQL is never rolled backward. A
database restore that loses committed Tether state breaks that assumption: side effects made in
the lost interval cannot be reconstructed from the database, and dispatch must stay paused under
operator control until the situation is understood. See
[PostgreSQL recovery](operations/postgres-recovery.md). No restore drill has been performed.

## Proven guarantees

Each entry gives the guarantee, the condition it depends on, and its main limit. The link label is
the entry's identifier on the card, where the full evidence is listed.

### Authorization before effect

**No side effect without committed authorization and dispatch records**
([G-1](product/tether-v1-guarantees.md#g-1-no-external-side-effect-without-committed-authorization-and-dispatch-records)).
A tool runs only after a ledger entry for that exact call was created under the current policy,
and after the entry was marked `dispatched`, with its audit event, in a transaction that committed
before the external call. The worker executes only arguments whose call hash equals the ledger's.

- *Depends on:* the tool being executed by Tether's worker; graph nodes never perform side effects.
  A replay that finds persisted state recorded for a different call fails closed with
  `REPLAY_DIVERGENCE` instead of adopting it.
- *Limit:* a refusal at the pre-dispatch re-check writes no audit event of its own.

**Decisions are made only under the workspace's active policy version**
([G-15](product/tether-v1-guarantees.md#g-15-decisions-are-made-only-under-the-workspaces-active-policy-version)).
A policy decision or eligibility answer is recorded only if the deciding process's loaded policy
version equals the workspace's active version, read in the same transaction. Otherwise nothing is
recorded: a worker requeues the job without consuming an attempt, and the API returns `503`.

- *Depends on:* decisions taking only trusted inputs (the requester's recorded identity and roles,
  the run's agent and workspace, the tool's registered risk and permissions), never model output.
  `tether bootstrap apply` is the only writer of policy versions.
- *Limit:* a stale process makes no decision at all. Its job is retried later, and an API caller
  gets `503` and must retry.

See [Policy](governance/policy.md).

### At-most-once and idempotent re-sends

**A non-idempotent write is sent at most once**
([G-2](product/tether-v1-guarantees.md#g-2-a-non-idempotent-write-is-sent-at-most-once)).
Once a non-idempotent write has been dispatched, Tether never re-sends it automatically. Any
unknown outcome becomes `indeterminate`.

- *Depends on:* the tool author's declaration. A write declares `idempotency="unsupported"`, and
  Tether trusts it. What the handler does inside one invocation is the tool's responsibility.
- *Limit:* worker death, lease expiry or a dropped connection after dispatch leaves the outcome
  unknown; the run then waits for its resolution. This includes a crash after the dispatch record
  commits but before the request leaves: the entry is `indeterminate` although nothing was sent.

**Idempotent writes are re-sent only with the same key, within a bound, and never after a stop**
([G-3](product/tether-v1-guarantees.md#g-3-idempotent-writes-are-re-sent-only-with-the-same-key-within-a-bound-and-never-after-a-stop)).
Retries and redeliveries of one logical execution carry the same deterministic idempotency key,
share one attempt counter bounded by the tool's descriptor, and each re-send is preceded by a
committed record that observed no stop.

- *Depends on:* the target honouring the forwarded key and retaining it longer than recovery takes.
- *Limit:* two sends with the same key can be in flight at once, for example after a lease expires
  during a slow send. Applying once is then the target's deduplication, not Tether's.

See [Side effects and the execution ledger](governance/side-effects-and-ledger.md).

### Honest unknown outcomes

**Unknown outcomes are never reported as failure**
([G-4](product/tether-v1-guarantees.md#g-4-unknown-outcomes-are-never-reported-as-failure-at-the-baseline-only-a-human-resolves-them)).
After dispatch, a write's result is `not_applied` only when the tool raises `ToolNotApplied`; any
other result that is not a success is `unknown`, and the ledger entry becomes `indeterminate`. (A
call refused before anything was sent is also `not_applied`.) Where a read-back is declared, a `verified_not_applied` result is the read-back's
verdict, which may rest on absence (see the next section) and is not covered by this guarantee.
The model is never told an unknown write failed. An eligible reconciler (the `reconciler` role, eligible under policy, and not the run's
requester) resolves the entry with a justification, recorded as an audit event before the run
resumes or ends.

- *Depends on:* the tool raising `ToolNotApplied` only when it is certain nothing was applied.
  Tether trusts that declaration.
- *Limit:* the card states this guarantee for human resolution. Automatic verification, which can
  resolve an entry first, is implemented and tested but not yet part of this guarantee (see the
  next section). With no eligible reconciler, the run waits.

**Job exhaustion always produces a domain outcome**
([G-10](product/tether-v1-guarantees.md#g-10-job-exhaustion-always-produces-a-domain-outcome)).
A job delivered past its maximum attempts is marked dead in the same transaction as an outcome for
what it was doing: an undispatched tool call is `not_applied`, a dispatched write `indeterminate`,
a dispatched read a typed failure; a run with an unresolved entry is held for reconciliation,
otherwise it ends `failed`. A wake is always enqueued.

- *Limit:* a timer job is only marked dead with a wake; its condition is still enforced when the
  next decision is made. The delivery counts are development defaults. A dispatched entry whose
  tool version can no longer be resolved is outside this guarantee.

See [Unknown outcomes](governance/unknown-outcomes.md) and
[Execution and recovery](architecture/execution-and-recovery.md).

### Stops

**After a stop, no model call starts and no proposal is governed**
([G-5](product/tether-v1-guarantees.md#g-5-after-a-stop-no-model-call-starts-and-no-proposal-is-governed)).
Once cancellation is requested or the stored deadline has passed, Tether starts no further model
call and governs no further proposal. A stopped run whose write is unresolved waits; resolving it
then only records the outcome and ends the run.

- *Depends on:* the stop being committed. A model request that started before the stop committed
  is not a further call; its proposals are recorded and discarded.
- *Limit:* the status becomes `cancelled` or `timed_out` only when the run driver next runs. Until
  then, clients see the stop flags next to the old status.

**Dispatched writes are never recalled, and unknown outcomes are never hidden by a clean terminal
status**
([G-6](product/tether-v1-guarantees.md#g-6-dispatched-writes-are-never-recalled-and-unknown-outcomes-are-never-hidden-by-a-clean-terminal-status)).
A stop never interrupts or recalls a dispatched write. A run is never shown as cleanly `cancelled`
or `timed_out` while an outcome is unknown, and its summary names every reconciled outcome.

- *Limit:* an unknown outcome after a stop holds the run in `awaiting_reconciliation` until it is
  resolved.

See [Run lifecycle](architecture/run-lifecycle.md).

### Single writer and terminal statuses

**Terminal run statuses never change**
([G-7](product/tether-v1-guarantees.md#g-7-terminal-run-statuses-never-change)).
Once a run is `succeeded`, `failed`, `cancelled`, `timed_out` or `abandoned`, a database trigger
forbids any change to its status, independently of application code.

- *Depends on:* the application database role, which cannot delete runs or disable the trigger.
  The migrator role and the database superuser can alter the schema, as for any database.

**At most one run driver changes a run's authoritative state at a time**
([G-8](product/tether-v1-guarantees.md#g-8-at-most-one-run-driver-changes-a-runs-authoritative-state-at-a-time)).
Driving a run is serialised by a session-level advisory lock and a per-run fencing token. A driver
that lost its lock cannot write, and a contended job is requeued without consuming an attempt.

- *Limit:* the LangGraph checkpoint is not fenced. A lock lost at the wrong moment can leave a
  stale checkpoint; that costs progress, not safety, because a replay that disagrees with the
  persisted rows fails closed.

### Crash safety

**A crash after a critical commit, during a model call or with a write in flight loses no run and
duplicates no idempotent write**
([G-9](product/tether-v1-guarantees.md#g-9-a-crash-after-any-critical-commit-during-a-model-call-or-with-a-write-in-flight-loses-no-run-and-duplicates-no-idempotent-write)).
A crash of the API or a worker right after any of the critical transactions from submission to the
final status commits, during a model call, or while an idempotent write is in flight leaves a run that resumes to the same outcome, with no
duplicate ledger entries and one applied effect per idempotent write at a target that honours the
key. Hard-killing and restarting the real processes mid-run has the same result.

- *Depends on:* idempotent writes against key-honouring targets. A non-idempotent write in flight
  during a crash becomes `indeterminate` instead.
- *Limit:* the chaos suite runs once per push in CI. Repeated 20 times in a row, it passed 19
  iterations; the failing one spanned a host suspend. That is not a clean 20 of 20. The crash
  points are a test harness; production processes carry none. Reconciliation, cancellation,
  deadline, dead-letter and verification transactions are not among the crash points this entry
  covers.

### Identity and workspace isolation

**Invalid credentials fail before any state change; out-of-scope resources do not exist**
([G-11](product/tether-v1-guarantees.md#g-11-invalid-credentials-fail-before-any-state-change-out-of-scope-resources-do-not-exist)).
On the submit, read, cancel, approve, reject, reconcile and admin-cancel routes, every credential
failure returns `401` before any audit, policy or job activity. The expected token audience comes
from the route, never from the token. A resource outside the caller's workspace or authorization
returns `404`, never `403`.

- *Depends on:* the development identity shortcut being inert. It works only when `TETHER_ENV` is
  `dev` or `test` and the request has no `Authorization` header.
- *Limit:* the same behaviour on the approval list and detail and the run views is tested but
  listed in the next section.

**Every request-path query after authentication is workspace-scoped**
([G-16](product/tether-v1-guarantees.md#g-16-every-request-path-query-after-authentication-is-workspace-scoped)).
Request handlers reach data only through repositories that cannot be built without a workspace
scope. Reads are filtered by it, writes are stamped with it, and composite foreign keys enforce
workspace consistency in the schema.

- *Limit:* the credential lookups that establish the workspace, and the worker's job lease, are the
  only unscoped queries. LangGraph checkpoints are keyed by run only.

See [Authentication and authorization](security/authentication-and-authorization.md).

### Exact-call approvals

**An approval is bound to exactly the call it was given and decided only by an eligible approver**
([G-12](product/tether-v1-guarantees.md#g-12-an-approval-is-bound-to-exactly-the-call-it-was-given-and-decided-only-by-an-eligible-approver)).
An approval carries an HMAC-SHA-256 call hash over the canonical (RFC 8785) envelope of the
validated arguments. Only a person with the core `approver` role and a role named by the tool's
policy rule, who is not the requester, can decide it, once, before it expires. A write that needs
approval executes only after an approval was recorded `approved`, and only with arguments whose
hash equals the ledger's.

- *Depends on:* the call's pinned tool version; descriptor metadata is bound through that version.
- *Limit:* the re-check just before dispatch (policy now denies, the call changed, the approval
  lapsed) is not part of this guarantee; it is listed in the next section.

See [Approvals](governance/approvals.md).

### Audit

**Critical audit events are written inside the state-changing transaction and chained per run**
([G-13](product/tether-v1-guarantees.md#g-13-critical-audit-events-are-written-inside-the-state-changing-transaction-and-chained-per-run)).
Every policy decision, skipped call, approval request and decision, dispatch, tool outcome,
reconciliation, stop, approval closure or expiry, replay divergence, dead-letter and terminal
status change is appended in the same transaction as the state it describes; if the append fails,
the change does not happen. Each run's events form an HMAC-SHA-256 chain keyed outside the
database, and in-place modification of an event is detected by anyone verifying with the audit key,
unless the modifier also holds that key. Run inspection (`audit_chain_valid`) recomputes the chain
only; `tether audit verify` also compares the persisted head.

- *Limit:* the chain is per run. Removing a run's last events is invisible to run inspection.
  `tether audit verify` exposes it only while the persisted head is left alone; a database
  administrator who also rewrites the head (no key needed), or removes a whole run, is not
  detected. A holder of the audit key can rewrite a chain that verifies. Events written with MAC
  version 1 do not cover their timestamp. The audit log is not WORM storage.

See [Audit](governance/audit.md).

### Masking

**Tool output and approval arguments are masked at one choke point, and secrets and payloads never
reach logs, spans or metric labels**
([G-14](product/tether-v1-guarantees.md#g-14-tool-output-and-approval-arguments-are-masked-at-one-choke-point-and-secrets-and-payloads-never-reach-logs-spans-or-metric-labels)).
Tool output and approval arguments pass through one masking function before they are persisted or
audited, and it replaces every field marked `Sensitive` with a fixed mask. Registered secrets and
credential shapes are scrubbed from log lines, tracebacks carry no local variables, and graph
state, job payloads, task input and tool arguments never reach logs, spans or metric labels.

- *Depends on:* the tool author marking sensitive fields. An unmarked field is not masked.
- *Limit:* unmasked arguments exist in LangGraph checkpoints and job payloads. Non-sensitive
  (masked) tool output placed in model context leaves the deployment when a hosted provider is
  used.

See [Secrets and data handling](security/secrets-and-data-handling.md).

## Implemented and tested, wording under review

The behaviours below exist in the code and pass automated tests. None of them is a guarantee yet:
each becomes one only when its wording is accepted onto the card. The card's second section lists
the evidence for each.

| Behaviour | What the tests show | Known limits |
|---|---|---|
| Automatic verification of unknown outcomes | An `indeterminate` write whose tool declares a read-back is resolved only after a committed gate that observed no stop and a policy `allow`; the probe never re-sends the write; a person's decision wins over a late probe | That a third-party probe only reads cannot be proven by Tether; any policy change between a write and its probe sends the entry to a person. See [Unknown outcomes](governance/unknown-outcomes.md) |
| Pre-dispatch re-check | A call whose policy now denies it, whose hash changed, or whose approval is missing, bound to another hash or lapsed is refused with no ledger entry and nothing sent | The refusal writes no audit event of its own; the changed-call and no-matching-approval cases are reached through test-only wrappers |
| End-to-end masking of `Sensitive` fields | Masked in the ledger, the approval, the audit payloads, run inspection and every approval view; a sink scan finds the output value nowhere | Proven with a test-only `Sensitive` tool; the reference workloads declare no `Sensitive` field. Arguments remain unmasked where described under G-14 |
| Configuration loss or a pinned version removed after dispatch never hides an unresolved effect | A worker that lacks a run's agent makes no model call, authorization or send, and holds the run while any entry is unresolved; a dispatched entry whose pinned version is gone is recorded unknown and never re-sent | |
| Lease heartbeats | A healthy tool call longer than the lease is neither redelivered nor made `indeterminate` | A synchronous, blocking handler starves the heartbeat and loses its lease |
| Repair and denial bound | The run ends `failed` (or `cancelled`/`timed_out` if a stop is active), after any unresolved entry resolves, once invalid proposals or denials exceed their budget | |
| Per-tool approval expiry | Each tool's rule may set its own expiry, bounded by a deployment maximum; a value above the maximum is refused | The default (3600 s) and maximum (86400 s) are proposed values, not yet confirmed |
| Approval list, detail and approver run view; full authorization matrix | Every route, audience, role and workspace combination behaves as specified, in both directions between two workspaces | Not covered: an approval-detail access event, and the run view for an approver who later lost eligibility |
| Second, unrelated workload | Repository change control runs on the same core with no core change | Proven over a mocked GitHub transport only |
| Fail-closed production configuration | Processes refuse to start without an explicit valid `TETHER_ENV`; development components are unavailable in `prod` | The development identity module still ships in the production image, inert |

### Implemented, proven in CI over mocked transports only, run live once

These are not candidates for a guarantee yet.

| Behaviour | What the tests show | Known limits |
|---|---|---|
| Provider retry and the OpenAI adapter | Bounded, stop-gated provider retry; the same run executes on the scripted provider and on OpenAI by configuration | CI covers the adapter over a mocked transport only; one live run |
| Governed write against a real external API (a GitHub sandbox) | Approval, at-most-once send, outcome classification, read-back and audit behave as on the demo target | CI covers the GitHub client over a mocked transport only; one live run |

## What Tether does not guarantee

- **Exactly-once for arbitrary APIs.** At-most-once holds for non-idempotent writes. Idempotent
  re-sends rely on the target honouring the idempotency key.
- **That a tool's declarations are true.** Tether trusts a tool's `idempotency`, `ToolNotApplied`,
  `Sensitive` annotations and read-back. A tool that misdeclares them can break the guarantees
  above.
- **That a read-back only reads.** This is the tool author's contract. It is tested against the
  two reference targets only.
- **Tamper-proof audit storage.** The chain detects modification and reordering within a run by a
  party without the audit key. It does not detect deletion of a chain's tail with its head, or of
  a whole run, and it is not WORM storage or a compliance product. No cross-run audit query exists.
- **Deduplication of submissions.** A run submission carries no client idempotency key. A caller
  that retries a submission whose response was lost creates a second run, which may repeat the same
  side effects.
- **Mid-run revocation.** Removing a requester's access during a run is not guaranteed to stop it;
  exposure is bounded by approval expiry and the run deadline.
- **Operating limits.** No concurrency, latency, throughput or tool-duration limit is published.
  The measurements in [Validation](validation.md) are single-host figures, not limits.
- **Database recovery objectives.** PostgreSQL is a single point of failure. While it is down
  nothing progresses and no committed state is lost. A write in flight when the database goes away
  can end `indeterminate`, and buffered access events can be lost. This has no integration test
  against a real outage. Recovery point and time objectives are not set.
- **Production service identities.** Production runs require a human end-user token. There are no
  production service principals and no OIDC or JWKS discovery.
- **Protection against prompt injection, or a sandbox for tool code.** Tether governs what a tool
  call may do; it does not inspect model text for intent, and tool code runs inside the worker
  process without isolation.
- **Encryption at rest or data retention.** Checkpoints and job payloads hold unmasked arguments;
  nothing is deleted, and no retention policy exists.
- **Features that are designed or planned but not built:** provider failover, an approval user
  interface, budgets, cost estimates and key-lifecycle automation.

## Related

- [Guarantee card](product/tether-v1-guarantees.md)
- [Validation](validation.md)
- [Evidence map](testing/strategy.md)
- [Security model](security/security-model.md)
