# Runbooks

Procedures and troubleshooting for operating Tether: what to do when a run is stuck, an outcome is unknown, a job is dead or a process will not start. It is written for operators. It says what to do; the linked pages say why.

Examples use synthetic values (`ws-example`, `<run-id>`). Hosts, tokens and keys are placeholders. Responses are described by their fields, not reproduced. Authentication in these procedures means real credentials: a bearer token for the route's audience and, on `/v1/tasks`, the workspace API key in `X-API-Key`. The development stack also accepts a stated identity; see [getting started](../development/getting-started.md).

## Symptom index

| What you see | Likely cause | Go to |
|---|---|---|
| Run is `awaiting_approval` | An approval is pending, expired, or no eligible approver has seen it | [A run stuck in awaiting_approval](#a-run-stuck-in-awaiting_approval) |
| Run is `awaiting_reconciliation` | A write's outcome is unknown, or a job was dead-lettered | [A run in awaiting_reconciliation](#a-run-in-awaiting_reconciliation) |
| Run is `queued` or `running` for a long time | No worker, workers saturated, or a stale policy | [A run that does not progress](#a-run-that-does-not-progress) |
| Run is `failed` with `system_error` | A job used all its deliveries | [A dead job](#a-dead-job) |
| Run is `failed` with `configuration_unavailable` | The worker has no configuration for the run's workspace or agent | [A worker that will not start](#a-worker-that-will-not-start) |
| Run is `failed` with `repair_budget_exhausted` or `denial_budget_exhausted` | The model kept proposing invalid or denied calls | [Inspecting a run](#inspecting-a-run) |
| `404` on a route you should be able to use | Wrong workspace, missing role, not eligible, or an unknown id | [Credential and authorization failures](#credential-and-authorization-failures) |
| `401` | Missing, expired or wrongly addressed credential | [Credential and authorization failures](#credential-and-authorization-failures) |
| `503` with `policy is reloading` | The active policy version differs from the one loaded | [503 from approval or reconciliation routes](#503-from-approval-or-reconciliation-routes) |
| Worker exits at start-up, or the container is unhealthy | Missing environment, bad bootstrap file, high-risk tool allowed | [A worker that will not start](#a-worker-that-will-not-start) |
| `audit_chain_valid` is `false`, or `tether audit verify` prints `INVALID` | Stored audit data fail verification | [Verifying a run's audit chain](#verifying-a-runs-audit-chain) |

## Inspecting a run

Three views, each for a different person. All show authoritative rows with masked payloads, never the task input of someone else's run and never raw secrets. Every view includes `audit_chain_valid`, computed on read.

| View | Request | Who | Shows |
|---|---|---|---|
| Requester | `GET /v1/tasks/runs/<run-id>` with `Authorization: Bearer <tasks-token>` and `X-API-Key: <your-key>` | The run's requester only | Status, `outcome_summary`, deadline, cancel fields, approvals with masked arguments, ledger entries, audit event list |
| Approver | `GET /v1/approvals/runs/<run-id>` with an approvals token | An `approver` who is eligible for at least one approval on the run | The same, plus each approval's tool version, call hash, requester and expiry. No task input |
| Operations | `GET /v1/ops/runs/<run-id>` with an ops token | `auditor` or `workspace_admin` | The requester's view, for any run in the workspace |

Anyone else, another workspace and an unknown id all get `404`. The approver and operations views record an access event (who inspected which run); the requester's own view does not.

Read a run in this order: `status` and `outcome_summary`; `approvals` (what is waiting on a person); `ledger` (what was sent and what is unresolved); `audit` (the sequence of events). Ledger entries show `state`, `attempts`, `outcome_reason`, `resolution` and `resolved_by`. While automatic verification runs, an `indeterminate` entry shows `verification: in_progress` and `verification_attempts`. The event list shows types, actors and tools, not payloads. Payloads are in `tether.audit_events.payload_masked`; see [audit](../governance/audit.md).

Some `outcome_summary` values and their meaning:

| Summary | Meaning |
|---|---|
| `system_error` | A job was dead-lettered with nothing in flight |
| `configuration_unavailable` | The worker has no configuration for the run's workspace or agent |
| `repair_budget_exhausted`, `denial_budget_exhausted` | The run exceeded `TETHER_MAX_REPAIRS` or `TETHER_MAX_DENIALS` |
| `replay_divergence` | Persisted state did not match its recorded hash; the run fails closed and sends nothing further |

A stop (cancel or deadline) can add its own reason to the summary. Run states are in [run lifecycle](../architecture/run-lifecycle.md).

## Finding and deciding approvals

1. List what you can decide: `GET /v1/approvals/approvals?limit=50` with an approvals token. Items are pending approvals you are eligible for, oldest first, each with masked arguments, `call_hash`, `requester`, `agent_id`, `expires_at` and `actionable`. If `next_after` is set, pass it as `after=<id>` for the next page.
2. Open one: `GET /v1/approvals/approvals/<approval-id>`. It works for any status, if you are eligible. Read the tool, its masked arguments and its preconditions. The approval binds exactly this call: a changed call needs a new approval.
3. Decide: `POST /v1/approvals/<approval-id>/approve` or `/reject`, with body `{"reason": "<up to 500 characters>"}`. The response is `{id, status}`.

| Response | Meaning |
|---|---|
| `200` | Decided. The run is woken |
| `404` | Unknown, another workspace, you lack the `approver` role, your role is not in the tool's `approver_roles`, or you are the run's requester |
| `409` | No longer pending: already decided, expired, closed, or the run has stopped or ended |
| `503` | Stale policy; see [below](#503-from-approval-or-reconciliation-routes) |

An approver is never the requester of the same run. The list is advisory; the decision route re-checks everything. See [approvals](../governance/approvals.md).

## A run stuck in awaiting_approval

1. Inspect the run and find the approval with status `pending`. Note `expires_at`.
2. **Not yet expired.** Nobody eligible has decided. Check that someone holding the `approver` role and a role named by the tool's rule exists, and that they are not the requester. Ask an approver to list approvals. If their list is empty although the run has a pending approval, they are not eligible for it, or the policy is stale (`503`).
3. **Past `expires_at`.** A timer job (`expire_approval`) moves the approval to `expired`, and the run receives a typed `expired` result and continues; the model may propose the call again, which needs a new approval. If the approval stays `pending` past its expiry, workers are not running or are far behind. Check worker health and the job state:

   ```sql
   SELECT kind, state, attempts, max_attempts, run_after, lease_expires_at
   FROM tether.jobs WHERE run_id = '<run-id>' ORDER BY created_at;
   ```

   Run this with a role that can read the `tether` schema, such as the migrator role. A decision on an expired approval is refused (`409`) even if the timer has not yet run.
4. To abandon the wait, cancel the run; see [cancelling a run](#cancelling-a-run). Cancelling closes pending approvals.

## A run in awaiting_reconciliation

A run is held in this state while any of its ledger entries is `pending`, `dispatched` or `indeterminate`. Tether never reports an unknown write as failed. Why, and how automatic verification works, is in [unknown outcomes](../governance/unknown-outcomes.md).

1. Inspect the run. Find the entry with state `indeterminate`.
   - `verification: in_progress`: automatic verification is still probing the target, each probe after a backoff. Wait. A person's resolution would win over a late probe, but there is usually no need.
   - No verification in progress and no resolution: verification ended without an answer (inconclusive probes used up, the tool declares none, or policy did not allow the probe). A person must resolve it.
   - Entry is `pending` or `dispatched` instead: the run is held because a job was dead-lettered. See [a dead job](#a-dead-job).
2. **Find the ledger entry id.** The HTTP views do not return it. Read it from the database with a role that can read the `tether` schema:

   ```sql
   SELECT id, step_ordinal, tool, effect, state, attempts, outcome_reason, idempotency_key
   FROM tether.ledger_entries
   WHERE run_id = '<run-id>' AND state = 'indeterminate';
   ```

3. **Establish the truth at the target.** Look at the real system, not at Tether. Use the tool name and the inspection view's masked arguments to decide whether the effect happened. The `idempotency_key` is what Tether sent, if the target records it. Do not guess: an unknown outcome is exactly the case where Tether cannot know.
4. **Reconcile.** `POST /v1/ops/ledger/<ledger-id>/reconcile` with an ops token whose roles include `reconciler`:

   ```json
   {"outcome": "applied", "justification": "Checked the target: the change exists."}
   ```

   `outcome` is `applied`, `not_applied` or `abandon`. `justification` is required, 1 to 2000 characters, and is recorded.

| Outcome | Entry becomes | Effect on the run |
|---|---|---|
| `applied` | `completed`, resolution `reconciled_applied` | The model sees the call as `applied` and the run continues |
| `not_applied` | `failed`, resolution `reconciled_not_applied` | The model sees `not_applied`. The rest of that model turn is skipped. A re-proposal is a new call with a new key and passes governance, including approval, again |
| `abandon` | `abandoned`, resolution `reconciled_abandon` | The run ends `abandoned`; no further model call |

If a cancel or deadline was already in effect, reconciliation only lets the run end `cancelled` or `timed_out`, with the reconciled outcome named in its summary. The response is `{ledger_id, state, resolution}`.

| Response | Meaning |
|---|---|
| `200` | Reconciled, or the same outcome was already recorded (no change) |
| `404` | Unknown id, another workspace, you lack `reconciler`, your role is not in `reconciler_roles`, or you are the run's requester |
| `409` | The entry is not awaiting reconciliation, or a different outcome was already recorded, or the run has ended |
| `422` | `justification` missing or empty, or `outcome` not one of the three |
| `503` | Stale policy |

The reconciliation is audited (`RECONCILED`, with your subject and justification) and wakes the run. Tether never calls the tool again from this route.

## A dead job

A job is delivered a bounded number of times: 8 for `advance_run`, 5 for `execute_tool` and `verify_outcome`, 10 for the two timers. A delivery that would exceed the bound ends the job as `dead` instead of running it, and the same transaction records what that means for the run. You see `tether.jobs.dead_lettered`, a `run dead-lettered` log line and a `DEAD_LETTERED` audit event.

| Dead job | What happened to the run |
|---|---|
| `advance_run` | The run becomes `awaiting_reconciliation` if a ledger entry is unresolved, otherwise `failed` with `system_error` |
| `execute_tool` | A `pending` entry becomes `failed` (nothing was sent). A `dispatched` write becomes `indeterminate`. A `dispatched` read becomes a typed failure. The run is then woken |
| `verify_outcome` | Verification ends; the entry stays `indeterminate` for a person |
| `run_deadline`, `expire_approval` | The job is marked dead and the run is woken. The condition is still enforced at decision time |

Procedure:

1. Find the dead jobs:

   ```sql
   SELECT id, kind, run_id, attempts, max_attempts, created_at
   FROM tether.jobs WHERE state = 'dead' ORDER BY created_at DESC LIMIT 50;
   ```

2. Inspect the run it belongs to. If it is `awaiting_reconciliation`, follow [that procedure](#a-run-in-awaiting_reconciliation).
3. Find why it exhausted its deliveries. Search worker logs for the run id: repeated `job processing failed` lines show the error type; repeated `lease lost` lines show database or worker starvation. Fix the cause (a bad dependency, a database problem, a tool that always fails).
4. A `failed` run is terminal and never changes. Submit a new run if the work should be redone.

## Verifying a run's audit chain

Every inspection view reports `audit_chain_valid`. To check one run directly, use the CLI. It is read-only and needs `TETHER_ENV`, `TETHER_DATABASE_URL` and the audit keys in its environment:

```
tether audit verify --workspace ws-example --run <run-id>
```

| Exit code | Output | Meaning |
|---|---|---|
| `0` | `VALID`, with `events`, `last_seq`, `head` and `mac_versions` | The chain recomputes and the stored head agrees |
| `1` | `INVALID`, with `reason`, `failed_seq`, `events` | Stored data fail verification |
| `2` | message on stderr | Usage or configuration error; nothing was verified |
| `3` | message on stderr | The database or an audit key was unavailable. Nothing can be said about the chain |
| `4` | `NOT_FOUND` | No events for that run in that workspace (an unknown run and another workspace look the same) |

`reason` is one of `sequence_mismatch` (an event is not at the next sequence number), `prev_mac_mismatch` (it does not link to its predecessor), `mac_mismatch` (a recomputed MAC differs), `unsupported_mac_version`, or `head_mismatch` (the stored head disagrees with the events). `failed_seq` names the first failing event.

What `INVALID` means: stored audit data were modified, reordered or removed, or the key used does not match. Before treating it as tampering, rule out an operational cause: exit code `3` is never reported as `INVALID`, but a retired audit key still referenced by old events can make verification fail. Check that every key ID the run's events used is still configured ([key lifecycle](key-lifecycle.md)). Preserve the database state, restore from a backup if you need to compare ([PostgreSQL recovery](postgres-recovery.md)), and treat it as a security incident if the cause is not operational. The chain detects modification and reordering within a run. It does not detect deletion of a chain's tail together with its head, or deletion of a whole run, and it is not write-once storage. See [guarantees](../guarantees.md).

## Cancelling a run

- **Requester:** `POST /v1/tasks/runs/<run-id>/cancel` with a tasks token and `X-API-Key`. Only the run's requester.
- **Workspace admin:** `POST /v1/ops/runs/<run-id>/cancel` with an ops token holding `workspace_admin`. Any run in the workspace. Approvers cannot cancel; they reject.

Both return `202` with `{run_id, status, cancel_requested_at, cancel_requested_by}`. Anyone else, another workspace or an unknown run gets `404`. Repeating the request, or cancelling a finished run, changes nothing.

A cancel is a request. It closes pending approvals and wakes the run, and the run driver ends the run `cancelled` at its next check. A write already in flight is never recalled: the run waits for its outcome first. If that outcome is unknown, the run holds in `awaiting_reconciliation` until the entry is resolved, and then ends `cancelled`. A run that reaches its deadline ends `timed_out` in the same way. See [run lifecycle](../architecture/run-lifecycle.md).

## A run that does not progress

1. Check workers: each container's health (`tether-worker-healthcheck`), the `tether.jobs.queued` gauge and `tether.job.pickup_latency` ([observability](observability.md)).
2. Look at the run's jobs with the query in [a run stuck in awaiting_approval](#a-run-stuck-in-awaiting_approval). A `ready` job past `run_after` means no worker is leasing; a `leased` job with a recent `lease_expires_at` is being worked on.
3. A worker whose loaded policy differs from the active version decides nothing and re-checks each second. If workers run but jobs are not completing, check for `503` on the approval routes; the cause is the same ([below](#503-from-approval-or-reconciliation-routes)).
4. If the database is unreachable, nothing progresses; see [deployment](deployment.md#when-postgresql-is-unavailable).

## A worker that will not start

A worker exits when it cannot start safely. Read its first log lines.

| Message or symptom | Cause | Fix |
|---|---|---|
| `tether: invalid configuration` followed by `TETHER_ENV must be set explicitly to one of: dev, test, prod` | `TETHER_ENV` absent or invalid | Set it. Exit code 2 |
| `... TETHER_DATABASE_URL is required` | Missing database URL | Set it. Exit code 2 |
| `no workload named '<name>' is installed` | The bootstrap file names a workload the image does not contain, for example the demo workload in the `prod` image | Remove the agent from the file or use an image that holds the workload |
| `policy allows high-risk tool <tool>` | A rule sets `allow` on a high-risk tool | Change the rule to `require_approval` or `deny`, apply, restart |
| `the default approval expiry ... is above the maximum`, or a rule's expiry above it | `TETHER_APPROVAL_TTL_SECONDS` or a rule's `approval_expiry_seconds` exceeds `TETHER_APPROVAL_MAX_TTL_SECONDS` | Lower the value or raise the maximum. Refused, never clamped |
| `TETHER_SETTINGS is invalid` under `tether: invalid configuration` | Among other things, `TETHER_WORKER_LEASE_HEARTBEAT_SECONDS` is not below `TETHER_WORKER_LEASE_SECONDS` | Lower the heartbeat interval or raise the lease |
| A provider configuration error naming `TETHER_OPENAI_MODEL` or the API key | `TETHER_MODEL_PROVIDER=openai` without a model or key, or `scripted` in `prod` | Set them; `prod` requires `openai` |
| Container unhealthy, process running | The heartbeat file is stale: the database is unreachable | See [deployment](deployment.md#when-postgresql-is-unavailable) |

The settings errors name variables, never values. A bootstrap file that is unreadable or invalid stops both `tether-api` and `tether-worker`. See [configuration](configuration.md).

A worker that **starts** but whose loaded policy is not the active version decides nothing. That is the next section.

## 503 from approval or reconciliation routes

`503` with `policy is reloading` means the policy version computed from the bootstrap file loaded by this API process differs from the version active in the database. Tether refuses to decide under a policy it is not sure is current. It also affects the approval list, detail and run-evidence routes, and it holds workers' decisions.

1. Compare the file the processes loaded with the file you applied. Typical causes: the file was edited but not applied, it was applied but the processes were not restarted, or a new release changed the evaluator version.
2. Apply the file: `tether bootstrap apply <path>` as the migrator role.
3. Restart every `tether-api` and `tether-worker` so they load the same file.
4. Retry. Nothing is lost: approvals stay pending and runs stay held.

## Credential and authorization failures

| Response | Meaning | Check |
|---|---|---|
| `401` | The credential is missing, malformed, expired, signed by an unknown key, issued for another audience, or not from a trusted issuer. No handler ran, so nothing was written | The token's `iss`, `aud` (must equal the route's audience), `exp`, `kid` and algorithm against the workspace's `issuers`; on `/v1/tasks`, that `X-API-Key` is valid and belongs to the token's workspace. In `prod`, a request with no credentials is always `401` |
| `404` | You are authenticated but the thing is not yours to act on: unknown, another workspace, missing role, not eligible under policy, or you are the requester acting as approver or reconciler | The token's `roles` claim and the policy's `approver_roles` or `reconciler_roles`; whether the actor is the requester |
| `422` on submit | The API key may run several agents and the request named none | Send `agent_id` |

Audiences are `tether-tasks` for `/v1/tasks`, `tether-approvals` for `/v1/approvals` and `tether-ops` for `/v1/ops`. A role check happens after authentication, so a valid token with the wrong role is `404`, not `403`. Details are in [authentication and authorization](../security/authentication-and-authorization.md).

## Rotating keys

Rotation is manual, by overlap, with restarts. Never remove an audit key that signed events you still want to verify, or a binding key that a non-terminal run still pins. The steps for audit, binding, API and issuer keys are in [key lifecycle](key-lifecycle.md). After changing a bootstrap-file credential (API key or issuer key), run `tether bootstrap apply` and restart.

## Related

- [Deployment](deployment.md)
- [Configuration](configuration.md)
- [Observability](observability.md)
- [Unknown outcomes](../governance/unknown-outcomes.md)
- [Approvals](../governance/approvals.md)
- [HTTP API reference](../reference/api.md)
