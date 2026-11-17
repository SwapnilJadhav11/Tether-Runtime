# Getting started

This page takes you from a fresh clone to a governed run that you approved yourself, on the development stack. It is for engineers who want to run Tether locally, read its behaviour first-hand, and then build on it.

The development stack runs with `TETHER_ENV=dev`. It is not a production deployment: see [deployment](../operations/deployment.md) for that.

## Prerequisites

| Tool | Version | Used for |
|---|---|---|
| Python | 3.12 (`.python-version`; the project requires `>=3.12,<3.13`) | the CLI, the tests and the scripts |
| [uv](https://docs.astral.sh/uv/) | current | dependency management and every `uv run` command below |
| Docker with Compose v2 | current | the development stack and the test databases |

Install the locked dependencies from the repository root:

```bash
uv python install
uv sync --locked
```

`uv run poe` lists every task the project defines.

## Create the environment file

The Compose stack reads `deploy/compose/.env`, which is git-ignored. Start from the example and replace every value:

```bash
cp deploy/compose/.env.example deploy/compose/.env
```

The values you must set:

| Variable | What it is | How to generate it |
|---|---|---|
| `TETHER_POSTGRES_PASSWORD` | PostgreSQL superuser password, used only to initialise the database | `python -c "import secrets; print(secrets.token_urlsafe(24))"` |
| `TETHER_MIGRATOR_DB_PASSWORD` | Password of the role that owns the schema; used only by the `migrate` service | same command |
| `TETHER_APP_DB_PASSWORD` | Password of the least-privilege role used by `api` and `worker` | same command |
| `TETHER_AUDIT_KEY_DEV1` | Key for the audit chain | `python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"` |
| `TETHER_BINDING_KEY_DEV1` | Key that binds an approval to an exact call | same command, run again |

Passwords are embedded in database URLs, so use only letters, digits, `-` and `_`. The two key values must be different: audit keys and call-binding keys are separate families and never share material ([ADR-0010](../adr/0010-keyed-canonical-call-hash.md)). Leave `TETHER_AUDIT_ACTIVE_KEY_ID` and `TETHER_BINDING_ACTIVE_KEY_ID` at `DEV1`; they name which key signs new records.

Leave `TETHER_MODEL_PROVIDER=scripted` for now. Never commit `.env`.

## Start the stack

```bash
uv run poe smoke
```

The task builds the image, starts every service, waits until each is healthy, and lists them. Compose runs these services:

| Service | Role |
|---|---|
| `postgres` | the only stateful dependency |
| `migrate` | one-shot: applies migrations, then applies `config/bootstrap/dev.yaml` (workspace `ops-demo`, agent `incident-remediator`, policy, issuers, API key) |
| `api` | the HTTP API on `http://localhost:8080` |
| `worker` | leases jobs and drives runs |
| `demo-target` | a small service that the incident workload restarts; its state is in memory |
| `otel-collector`, `jaeger` | traces; the Jaeger UI is on `http://localhost:16686` |
| `ollama` | started by the file, unused by Tether |

Only the API and the Jaeger UI are published to the host. The `api` and `worker` containers are built from the `dev` image target, which adds the development bootstrap file and the scripted-provider scenarios.

## The quick run

`scripts/smoke_submit.py` runs the whole flow against the stack: it submits a run as `alice`, waits for the approval, approves it as `bob`, and prints the final run view.

```bash
uv run python scripts/smoke_submit.py
```

It uses the **development identity shortcut**: the request body states who the caller is, instead of presenting credentials. The API accepts that only when a request has no `Authorization` header and `TETHER_ENV` is `dev` or `test`. In `prod` the same request is `401`. The shortcut exists so a first run needs no credentials. It is not how real callers authenticate, and the rest of this page does not use it.

The run uses the **scripted model provider**, which replays a fixed scenario from `workloads/incident_remediation/scenarios/restart_checkout.yaml` instead of calling a model:

1. read `get_service_health` for `checkout` (policy: `allow`);
2. propose `restart_service` for `checkout` (policy: `require_approval`);
3. after approval, report the result.

Expected output ends with the run view and `demo OK`. If a step fails, the script exits non-zero and names the status it saw.

## The same flow with real credentials

Real callers present a JWT and, on task routes, a workspace API key. You need three things that a fresh clone does not contain, because private keys are never committed: issuer keys, JWTs minted from them, and an API key.

### 1. Create issuer keys and an API key

`tools/dev_issuer/` is a development-only token issuer. It is not part of the Tether image. It writes private keys to `tools/dev_issuer/.keys/`, which is git-ignored.

```bash
uv run python tools/dev_issuer/keys.py ops-demo
uv run tether apikey new demo-app --agent incident-remediator
```

The first command creates one Ed25519 key per audience (`tether-tasks`, `tether-approvals`, `tether-ops`) and prints an `issuers:` block holding only the public halves. The second prints a raw API key, shown once, and a bootstrap entry (`key_id`, `salt`, `secret_hash`, `agents`). Copy the raw key somewhere safe.

Edit `config/bootstrap/dev.yaml` in two places:

- replace the workspace's `issuers:` block with the printed one;
- replace the `api_keys:` entry with the printed bootstrap entry.

Do not commit this edit: the committed values belong to keys you do not hold. The dev image copies `config/` at build time, so rebuild and re-apply:

```bash
uv run poe smoke
```

`tether bootstrap apply` runs again in the `migrate` service. Bootstrap makes the database match the file exactly: issuers and keys that are not in the file are removed.

### 2. Mint tokens

Each route group accepts only tokens for its own audience. Mint one token per person:

```bash
export REQUESTER_TOKEN=$(uv run python tools/dev_issuer/mint.py alice --aud tether-tasks --role operator)
export APPROVER_TOKEN=$(uv run python tools/dev_issuer/mint.py bob --aud tether-approvals --role approver)
export TETHER_API_KEY=<the raw key printed by tether apikey new>
```

A token lives 900 seconds by default (`--ttl` changes it). `alice` has the `operator` role, which `dev.yaml` maps to the permissions the incident tools require. `bob` has the `approver` role, which the policy rule for `restart_service` names. Mint again when a token expires.

Run `keys.py` before `mint.py`. If `mint.py` finds no key it creates one, and that key will not match the public keys in `dev.yaml`.

### 3. Submit a run

```bash
curl -s -X POST http://localhost:8080/v1/tasks/runs \
  -H "Authorization: Bearer $REQUESTER_TOKEN" \
  -H "X-API-Key: $TETHER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task_input": {"message": "checkout is down #scenario=restart_checkout"}}'
```

The response is `201`:

```json
{"run_id": "5b0c2f0e-0000-4000-8000-000000000001", "status": "queued"}
```

The workspace and agent come from the API key; the requester comes from the token. Nothing in `task_input` identifies anyone. Keep the run id:

```bash
export RUN_ID=<run_id from the response>
```

### 4. Find the pending approval

Poll the run until its status is `awaiting_approval` (a few seconds):

```bash
curl -s http://localhost:8080/v1/tasks/runs/$RUN_ID \
  -H "Authorization: Bearer $REQUESTER_TOKEN" -H "X-API-Key: $TETHER_API_KEY"
```

Only the requester can read this view. Then, as the approver, list what you may decide:

```bash
curl -s http://localhost:8080/v1/approvals/approvals \
  -H "Authorization: Bearer $APPROVER_TOKEN"
```

Illustrative response (ids and times differ):

```json
{
  "items": [
    {
      "id": "7d1e9a52-0000-4000-8000-000000000002",
      "run_id": "5b0c2f0e-0000-4000-8000-000000000001",
      "status": "pending",
      "actionable": true,
      "tool": "restart_service",
      "tool_version": "1.1.0",
      "arguments_masked": {"service": "checkout"},
      "preconditions_masked": {},
      "call_hash": "<hex>",
      "requester": "alice",
      "agent_id": "incident-remediator",
      "created_at": "2026-01-01T10:00:00Z",
      "expires_at": "2026-01-01T11:00:00Z",
      "decided_by": null,
      "decided_at": null,
      "decision_reason": null,
      "policy_version": null
    }
  ],
  "next_after": null
}
```

The list shows only approvals that you are eligible to decide. The approver in this example is not the requester, because nobody can approve their own run.

### 5. Approve

```bash
export APPROVAL_ID=<id from the list>
curl -s -X POST http://localhost:8080/v1/approvals/$APPROVAL_ID/approve \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "checkout is unhealthy; restart approved"}'
```

The response is `200` with `{"id": "...", "status": "approved"}`. The approval is bound to the exact call that you saw: if the call changed, the approval would not cover it ([approvals](../governance/approvals.md)). To refuse instead, post to `.../reject`.

To see the separation of duties, mint `alice` a token for `tether-approvals` with `--role approver` and repeat the request: the response is `404`, because a requester is never eligible to approve their own run.

### 6. Read the result

Poll the run until `status` is `succeeded`. The run view carries the evidence:

- `approvals`: the approval, who decided it, and when it expires;
- `ledger`: one entry per tool call with its `state` and `outcome`; the restart is `completed`;
- `audit`: the audit timeline, in order;
- `audit_chain_valid`: whether the run's audit chain verifies.

Illustrative `audit` event types for this run, abbreviated:

```text
RUN_SUBMITTED, POLICY_DECISION, EXECUTION_AUTHORIZED, DISPATCHED, TOOL_OUTCOME,
POLICY_DECISION, APPROVAL_REQUESTED, APPROVAL_DECIDED, EXECUTION_AUTHORIZED,
DISPATCHED, TOOL_OUTCOME, RUN_FINISHED
```

The first five events are the read. The rest are the restart: its policy decision, the approval request and decision, the authorization to execute, the dispatch, the outcome, and the end of the run. [Audit](../governance/audit.md) explains each event.

The approver can read the same evidence with `GET /v1/approvals/runs/$RUN_ID`, and a `workspace_admin` or `auditor` with `GET /v1/ops/runs/$RUN_ID`. See the [API reference](../reference/api.md).

## Verify the audit chain

`audit_chain_valid` in the API is a convenience. The authoritative check is the CLI, which recomputes the chain from the database. Run it inside the `api` container, which already has the database URL and the audit key:

```bash
docker compose -f deploy/compose/docker-compose.yml exec api \
  tether audit verify --workspace ops-demo --run $RUN_ID
```

Illustrative output for a valid chain:

```text
VALID
workspace: ops-demo
run: 5b0c2f0e-0000-4000-8000-000000000001
events: 12
last_seq: 12
head: seq=12 consistent
mac_versions: 2
```

Exit code `0` means `VALID`; other codes are listed in the [CLI reference](../reference/cli.md).

## Use a real model

Set the provider in `deploy/compose/.env`:

```text
TETHER_MODEL_PROVIDER=openai
TETHER_OPENAI_MODEL=<a model that supports tool calling>
TETHER_OPENAI_API_KEY=<your-key>
```

Then recreate the containers with `uv run poe smoke`. There is no default model: Tether never spends on a model nobody chose. Calls to the OpenAI API are paid. With a real model, write the task as plain language and drop the `#scenario=` marker; the model chooses the tool calls, and the same governance applies to them. [Model providers](../operations/model-providers.md) covers the settings, credential handling and error behaviour.

## Look around

- Logs: `docker compose -f deploy/compose/docker-compose.yml logs api worker`. They are JSON, one record per line.
- Traces: open `http://localhost:16686`. A run is one trace across the API and the worker.
- Metrics: `docker compose -f deploy/compose/docker-compose.yml logs otel-collector`. V1 prints metrics to the collector log and ships no dashboard.

## Stop the stack

```bash
docker compose -f deploy/compose/docker-compose.yml down        # keep the database volume
docker compose -f deploy/compose/docker-compose.yml down -v     # also delete it
```

The demo target keeps its state in memory, so restarting its container resets it.

## Next

Write your own tool in [writing a tool](writing-a-tool.md), run the checks in [testing](testing.md), and read the [architecture overview](../architecture/overview.md) for how the pieces fit.
