# HTTP API reference

This page documents every route of the Tether API: who may call it, what it accepts, what it returns, and what each status code means. It is for engineers integrating a calling application, an approval tool or an operations tool. Who may hold which role, and how credentials are issued, is in [authentication and authorization](../security/authentication-and-authorization.md).

## Conventions

**Base path.** All business routes start with `/v1/`. The API listens on port `8080` by default (`TETHER_API_PORT`). Bodies are JSON (`Content-Type: application/json`). Identifiers are UUIDs. Timestamps are ISO 8601 in UTC.

**Route groups and audiences.** Each group accepts credentials for one audience only. A token for another audience is a `401`.

| Group | Prefix | Audience | Used by |
|---|---|---|---|
| Tasks | `/v1/tasks` | `tether-tasks`, plus a workspace API key | the application that submits runs (the requester) |
| Approvals | `/v1/approvals` | `tether-approvals` | approvers |
| Operations | `/v1/ops` | `tether-ops` | reconcilers, auditors, workspace admins |
| Health | `/healthz`, `/readyz` | none | orchestrators and load balancers |

**Authentication.** Send `Authorization: Bearer <JWT>`. The token must name the workspace, a subject, its roles and the route's audience, be signed by an issuer registered for that workspace, and be unexpired. Task routes also need `X-API-Key: <key>`: the key must belong to the same workspace as the token, and it decides which agents the caller may run. Anything the request body states about identity is ignored when a token is present.

**Authorization comes after authentication.** A caller who has valid credentials but may not act on a resource gets `404`, never `403`: the API does not reveal whether a run, approval or ledger entry exists in another workspace or belongs to someone else.

**Status codes.**

| Code | Meaning |
|---|---|
| `200`, `201`, `202` | Success. `201` for a created run, `202` for an accepted cancel request. |
| `401` | Credentials are missing or invalid: no token, a malformed or expired token, a wrong audience, an unknown issuer or key, a missing or mismatched API key. Credential failures are `401`, raised before the handler runs. A malformed `dev_identity`, `agent_id` or `dev_workspace_id` value can yield `422` first. Either way nothing is evaluated or written. The body is `{"detail": "invalid or missing credentials"}`, with `WWW-Authenticate: Bearer`. |
| `404` | The resource does not exist, belongs to another workspace, or the caller is not allowed to act on it (wrong role, not the requester, ineligible approver). Indistinguishable by design. |
| `409` | The resource is no longer in a state that allows the action. |
| `422` | The request is malformed: a missing or invalid field, or an ambiguous agent. |
| `503` | The active policy version does not match the one this API process loaded (`policy is reloading`). Nothing was changed; retry after the configuration is applied everywhere. |

**Errors** are JSON: `{"detail": "..."}`. Validation errors (`422`) carry FastAPI's default list of `{type, loc, msg, input, ...}` entries. They can echo the submitted values back to the caller, so do not log a `422` body from a request that carried secrets.

**Development identity shortcut.** When `TETHER_ENV` is `dev` or `test`, a request that carries **no** `Authorization` header may state its own identity in the body (or, for reads, in a query parameter). This exists so a first run needs no credentials. It is refused in `prod` (`401`), and it never applies to the read routes under `/v1/approvals/approvals` and `/v1/approvals/runs`, or to `GET /v1/ops/runs/{run_id}`. Each route below says which body object it takes. Do not use it in an application.

**OpenAPI.** In `dev` and `test` the API serves its schema at `/openapi.json`. In `prod` it serves no schema.

## Tasks

### `POST /v1/tasks/runs`

Submit a run. The run row, its first audit event, its first `advance_run` job and its `run_deadline` timer job are written in one transaction; the response means the run is durably queued.

| | |
|---|---|
| Caller | The requester: a `tether-tasks` token plus `X-API-Key`. |
| Body | `task_input` (object, required): the task. If it has a string `message`, the model sees that text; otherwise the whole object as JSON. `agent_id` (string, optional): which agent to run. |
| Development shortcut | `dev_identity`: `{"workspace_id", "agent_id", "requester": {"sub", "roles"}}`. |

The workspace and the agent come from the credentials, never from `task_input`. With a key bound to exactly one agent, omit `agent_id`. If the key is bound to any other number of agents and no `agent_id` is given, the response is `422`. A named `agent_id` must be one the key is bound to (`401` otherwise).

Response `201`:

```json
{"run_id": "5b0c2f0e-0000-4000-8000-000000000001", "status": "queued"}
```

| Status | Meaning |
|---|---|
| `201` | The run is queued. |
| `401` | Invalid credentials; or `agent_id` is not bound to the API key. |
| `404` | The agent does not exist in the workspace. |
| `422` | No `task_input`; or no `agent_id` where one is required. |

Example:

```bash
curl -s -X POST http://localhost:8080/v1/tasks/runs \
  -H "Authorization: Bearer $REQUESTER_TOKEN" -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task_input": {"message": "checkout is down"}}'
```

A run starts with a deadline (`TETHER_RUN_MAX_DURATION_SECONDS`, default 3600). Submission is not idempotent: the API has no client-supplied idempotency key, so a client that retries after a lost response creates a second run. Record the `run_id` as soon as you have it, and check for an existing run before resubmitting.

### `GET /v1/tasks/runs/{run_id}`

Read a run: its status and the evidence behind it.

| | |
|---|---|
| Caller | The run's requester, with a `tether-tasks` token and `X-API-Key`. Nobody else, even in the same workspace. |
| Development shortcut | Query parameter `dev_workspace_id`. Reads any run in that workspace. |

Response `200` (a `RunView`):

| Field | Type | Meaning |
|---|---|---|
| `id` | UUID | The run. |
| `agent_id` | string | The agent that ran it. |
| `status` | string | One of `queued`, `running`, `awaiting_approval`, `awaiting_tool`, `awaiting_reconciliation`, `succeeded`, `failed`, `cancelled`, `timed_out`, `abandoned`. The last five are terminal and never change. |
| `outcome_summary` | string or null | A short reason or result for a finished or held run. |
| `deadline_at`, `created_at` | timestamp | |
| `cancel_requested_at`, `cancel_requested_by` | timestamp, string, or null | Set when a cancel was requested. |
| `deadline_exceeded_at` | timestamp or null | Set when the deadline passed. |
| `approvals` | list | `{id, step_ordinal, tool, status, arguments_masked, decided_by, expires_at}`. Status is `pending`, `approved`, `rejected`, `expired` or `closed`. |
| `ledger` | list | One entry per tool call; see below. |
| `audit` | list | `{seq, event_type, actor, tool, policy_version}`, in order. Payloads are not included. |
| `audit_chain_valid` | boolean | Whether the run's events chain and MACs verify when this response is built. It does not check the persisted head; use `tether audit verify` for that. If an audit key is missing, the inspection routes fail with a server error. |

A ledger entry has `step_ordinal`, `tool`, `effect` (`read` or `write`), `state` (`pending`, `dispatched`, `completed`, `failed`, `indeterminate`, `abandoned`), `attempts`, `outcome` (the masked result, or null), `outcome_reason`, `resolution`, `resolved_by`, `verification_attempts`, `verification` (`"in_progress"` while an automatic read-back is pending, otherwise null) and `verification_evidence` (masked, once a read-back resolved the entry). All payloads are masked: a field marked sensitive never appears.

Illustrative response for a finished run, abbreviated:

```json
{
  "id": "5b0c2f0e-0000-4000-8000-000000000001",
  "agent_id": "incident-remediator",
  "status": "succeeded",
  "outcome_summary": "checkout was unhealthy; restarted it after approval.",
  "deadline_at": "2026-01-01T11:00:00Z",
  "created_at": "2026-01-01T10:00:00Z",
  "cancel_requested_at": null,
  "cancel_requested_by": null,
  "deadline_exceeded_at": null,
  "approvals": [
    {"id": "7d1e9a52-0000-4000-8000-000000000002", "step_ordinal": 1,
     "tool": "restart_service", "status": "approved",
     "arguments_masked": {"service": "checkout"}, "decided_by": "bob",
     "expires_at": "2026-01-01T11:00:00Z"}
  ],
  "ledger": [
    {"step_ordinal": 1, "tool": "restart_service", "effect": "write",
     "state": "completed", "attempts": 1,
     "outcome": {"service": "checkout", "healthy": true, "restarts": 1},
     "outcome_reason": null, "resolution": null, "resolved_by": null,
     "verification_attempts": 0, "verification": null, "verification_evidence": null}
  ],
  "audit": [{"seq": 1, "event_type": "RUN_SUBMITTED", "actor": "alice",
             "tool": null, "policy_version": null}],
  "audit_chain_valid": true
}
```

| Status | Meaning |
|---|---|
| `200` | The view. |
| `401` | Invalid credentials. |
| `404` | Unknown run, another workspace, or not the caller's run. |

### `POST /v1/tasks/runs/{run_id}/cancel`

Request that a run stop. The API records the request; the run driver ends the run `cancelled` (the API never writes a run's status). Pending approvals are closed. A write already dispatched is not recalled: the run waits for its outcome.

| | |
|---|---|
| Caller | The run's requester, with a `tether-tasks` token and `X-API-Key`. |
| Body | none. Development shortcut: `{"dev_identity": {"workspace_id", "sub", "roles"}}`. |

Response `202`:

```json
{"run_id": "5b0c2f0e-0000-4000-8000-000000000001", "status": "running",
 "cancel_requested_at": "2026-01-01T10:05:00Z", "cancel_requested_by": "alice"}
```

`status` is the run's status at the time of the request, which is not yet `cancelled`. Cancelling twice, or cancelling a terminal run, changes nothing and still returns `202` with the recorded state.

| Status | Meaning |
|---|---|
| `202` | The request is recorded (or was already). |
| `401` | Invalid credentials. |
| `404` | Unknown run, another workspace, or not the requester. |

## Approvals

The caller of every approvals route needs a `tether-approvals` token with the `approver` role. Policy then decides what the approver may see and decide: the approver's role must be one the tool's rule names, and the approver must not be the run's requester.

### `POST /v1/approvals/{approval_id}/{action}`

Decide a pending approval. `action` is `approve` or `reject`. The decision applies to the exact call the approval was requested for; see [approvals](../governance/approvals.md).

| | |
|---|---|
| Body | `reason` (string, at most 500 characters, optional). Development shortcut: `dev_identity` as `{"workspace_id", "sub", "roles"}`. |

Response `200`:

```json
{"id": "7d1e9a52-0000-4000-8000-000000000002", "status": "approved"}
```

`status` is `approved` or `rejected`. The decision, its audit event and the job that wakes the run are written in one transaction.

| Status | Meaning |
|---|---|
| `200` | Decided. |
| `401` | Invalid credentials. |
| `404` | Unknown approval, another workspace, no `approver` role, not eligible under policy (including the requester themselves). |
| `409` | The approval is no longer pending: already decided, expired, closed by a stop, or its run ended. `{"detail": "approval is no longer pending"}`. |
| `422` | `action` is not `approve` or `reject`; `reason` is too long. |
| `503` | Policy is reloading. |

### `GET /v1/approvals/approvals`

List the pending approvals the caller may decide now, oldest first. Real credentials only: the development shortcut is refused (`401`).

| Query | Meaning |
|---|---|
| `limit` | Page size, 1 to 200, default 50. Other values are `422`. |
| `after` | An approval id: return the approvals after it. Use the previous page's `next_after`. An unknown id is `404`. |

Response `200`:

```json
{"items": [ <ApprovalDetail>, ... ], "next_after": null}
```

`next_after` is the id to pass as `after` for the next page, or `null` on the last page. The list is advisory: the decision route re-checks everything when the approver acts.

An `ApprovalDetail`:

| Field | Meaning |
|---|---|
| `id`, `run_id` | The approval and its run. |
| `status` | `pending`, `approved`, `rejected`, `expired` or `closed`. |
| `actionable` | `true` if it is pending, not expired and its run is still active. |
| `tool`, `tool_version` | The exact tool and version the approval covers. |
| `arguments_masked`, `preconditions_masked` | What the call does and what it assumes, with sensitive fields masked. |
| `call_hash` | The keyed hash that binds the approval to this exact call. |
| `requester`, `agent_id` | Who asked and which agent proposed it. |
| `created_at`, `expires_at` | When it was requested and when it lapses. |
| `decided_by`, `decided_at`, `decision_reason`, `policy_version` | Null until decided. |

The detail carries no model rationale: an approver sees trusted facts, not the model's argument for them.

| Status | Meaning |
|---|---|
| `200` | The page. |
| `401` | Invalid credentials, or no token. |
| `404` | No `approver` role; or `after` names an unknown approval. |
| `422` | `limit` out of range. |
| `503` | Policy is reloading; no data is returned. |

### `GET /v1/approvals/approvals/{approval_id}`

One approval, in any status, if the caller is eligible for it. Response `200` is an `ApprovalDetail`. Statuses: `401`, `404` (unknown, other workspace or ineligible), `503`. Real credentials only.

### `GET /v1/approvals/runs/{run_id}`

Evidence for a run that has at least one approval the caller is eligible for: the context an approver needs before deciding. Response `200` is an `ApproverRunView`: `id`, `status`, `outcome_summary`, `requester` (the subject only), `agent_id`, `created_at`, `deadline_at`, `cancel_requested_at`, `cancel_requested_by`, `deadline_exceeded_at`, `approvals` (a list of `ApprovalDetail`), `ledger`, `audit` and `audit_chain_valid`, with the same shapes as the run view above. It contains no task input. Statuses: `401`, `404` (unknown run, other workspace or no eligible approval), `503`. Real credentials only. Each call records an access event.

## Operations

### `POST /v1/ops/ledger/{ledger_id}/reconcile`

Resolve an `indeterminate` ledger entry: a person states what happened at the target. The tool is never called from here. The caller needs a `tether-ops` token with the `reconciler` role, a role the policy lists under `reconciliation.reconciler_roles`, and must not be the run's requester. See [unknown outcomes](../governance/unknown-outcomes.md).

| Body field | Meaning |
|---|---|
| `outcome` | `applied`, `not_applied` or `abandon`. |
| `justification` | Required, 1 to 2000 characters. Stored and audited. |

Development shortcut: add `dev_identity` as `{"workspace_id", "sub", "roles"}`.

Response `200`:

```json
{"ledger_id": "3c4f1b6e-0000-4000-8000-000000000003",
 "state": "completed", "resolution": "reconciled_applied"}
```

| `outcome` | `state` | `resolution` |
|---|---|---|
| `applied` | `completed` | `reconciled_applied` |
| `not_applied` | `failed` | `reconciled_not_applied` |
| `abandon` | `abandoned` | `reconciled_abandon` |

The resolution, a `RECONCILED` audit event and the job that wakes the run commit together.

| Status | Meaning |
|---|---|
| `200` | Resolved. Repeating the same outcome on an already resolved entry is also `200`, with no change. |
| `401` | Invalid credentials. |
| `404` | Unknown entry, another workspace, no `reconciler` role, or not eligible under policy. |
| `409` | The entry is not awaiting reconciliation, was resolved differently, or its run is terminal. |
| `422` | Missing or empty `justification`; invalid `outcome`. |
| `503` | Policy is reloading. |

If an automatic read-back resolves an entry first, a later reconciliation of it is `409`. If a person resolves it first, the person's decision stands.

### `GET /v1/ops/runs/{run_id}`

Inspect any run in the caller's workspace, read-only. The caller needs a `tether-ops` token with the `auditor` or `workspace_admin` role. Real credentials only. Response `200` is the same `RunView` the requester gets, with masked payloads. Each call records an access event. Statuses: `401`, `404` (no role, unknown run or another workspace).

### `POST /v1/ops/runs/{run_id}/cancel`

Cancel any run in the caller's workspace. The caller needs a `tether-ops` token with the `workspace_admin` role. Approvers cannot cancel; they reject. Body: none (development shortcut: `dev_identity`). Response `202` is the same `CancelRequested` object as the requester's cancel, with the same semantics. Statuses: `202`, `401`, `404` (no role, unknown run, another workspace).

## Health

Neither route needs credentials, returns data about runs, or appears in the OpenAPI schema.

| Route | Meaning | Responses |
|---|---|---|
| `GET /healthz` | Liveness: the process is serving. Does not touch the database. | `200` `{"status": "ok"}` |
| `GET /readyz` | Readiness: the database is reachable with the application role. | `200` `{"status": "ready"}`; `503` `{"status": "unavailable"}` |

## Related

- [Authentication and authorization](../security/authentication-and-authorization.md)
- [Approvals](../governance/approvals.md)
- [Unknown outcomes](../governance/unknown-outcomes.md)
- [CLI reference](cli.md)
- [Getting started](../development/getting-started.md): the routes in a worked run
