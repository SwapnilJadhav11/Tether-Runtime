# CLI reference

This page documents the `tether` command and the three process entry points: what each does, its options, the environment it needs, and its exit codes. It is for operators and engineers who migrate, configure and inspect a Tether deployment. Tether has no admin HTTP API: platform administration is the bootstrap file and these commands ([ADR-0014](../adr/0014-bootstrap-config-no-admin-api.md)).

| Command | Purpose | Connects to the database |
|---|---|---|
| `tether migrate` | Apply schema migrations and set up the LangGraph schema | yes, as the migrator role |
| `tether bootstrap apply PATH` | Apply a bootstrap file | yes, as the migrator role |
| `tether apikey new KEY_ID --agent AGENT` | Issue a workspace API key | no |
| `tether audit verify --workspace W --run ID` | Recompute and check one run's audit chain | yes, as the application role |
| `tether-api` | Run the API process | yes |
| `tether-worker` | Run the worker process | yes |
| `tether-worker-healthcheck` | Report whether the worker is alive | no |

All are installed as console scripts by the package. In a development checkout, prefix `tether` with `uv run`; in the Compose stack, run commands inside a container (`docker compose -f deploy/compose/docker-compose.yml exec api tether ...`).

## Required environment

`TETHER_ENV` has no default. Every command that connects to the database (`migrate`, `bootstrap`, `audit`) refuses to run unless `TETHER_ENV` is set to `dev`, `test` or `prod`. It prints the problem to standard error and exits `2`:

```text
tether: invalid configuration
  TETHER_ENV must be set explicitly to one of: dev, test, prod
```

`--help` never connects, so it is not gated. `tether apikey` is not gated either, because it only prints.

Two database roles are involved, and the CLI uses the right one for each command:

| Role | Variable | Used by |
|---|---|---|
| Migrator (owns the schema) | `TETHER_MIGRATOR_DATABASE_URL` | `migrate`, `bootstrap apply` |
| Application (least privilege, no DDL) | `TETHER_DATABASE_URL` | `audit verify`, `tether-api`, `tether-worker` |

Database URLs hold credentials and are never printed. Every setting is listed in [configuration](../operations/configuration.md).

## `tether migrate`

Applies every pending schema migration to the `tether` schema and sets up the LangGraph checkpoint schema (`langgraph`). It is idempotent: running it again changes nothing.

```text
tether migrate
```

| | |
|---|---|
| Options | `--help` only. |
| Environment | `TETHER_ENV`, `TETHER_MIGRATOR_DATABASE_URL`. |
| Output | `migrations applied` on success. |

| Exit | Meaning |
|---|---|
| `0` | Migrations applied (or already current). |
| `2` | `TETHER_ENV` is not set or invalid, or `TETHER_MIGRATOR_DATABASE_URL` is missing. |

Any other failure (an unreachable database, a failing migration) is an uncaught error and exits non-zero. Some migrations change how a writer must behave; the upgrade order is in [deployment](../operations/deployment.md).

In the Compose stack, the `migrate` service runs `tether migrate` and then `tether bootstrap apply` on every start.

## `tether bootstrap apply`

Validates a bootstrap file and applies it, as the migrator role, in **one transaction**: either all of it is written or none of it is.

```text
tether bootstrap apply PATH
```

| | |
|---|---|
| Argument | `PATH`: the bootstrap YAML file. |
| Options | `--help` only. |
| Environment | `TETHER_ENV`, `TETHER_MIGRATOR_DATABASE_URL`. Optionally `TETHER_APPROVAL_TTL_SECONDS` and `TETHER_APPROVAL_MAX_TTL_SECONDS`, which the file is checked against. |
| Output | `bootstrap applied: N workspace(s)` on success. |

What it writes, per workspace in the file:

- the workspace and its agents (added if missing; never removed);
- its issuers and API keys, **made to match the file exactly**: an issuer or key that is no longer in the file is deleted, which revokes it;
- the policy version, a hash of the policy section and the evaluator version. The new version becomes the one active version and the previous one is deactivated.

Applying the same file again changes nothing. The API and worker read the same file at startup (`TETHER_BOOTSTRAP_FILE`) and make decisions only under the version that is active in the database, so apply the file and then restart or roll the processes. See [configuration](../operations/configuration.md) for the file's structure and [policy](../governance/policy.md) for the policy section.

| Exit | Meaning |
|---|---|
| `0` | Applied. |
| `1` | The file is invalid (`invalid bootstrap file: ...`: unreadable, not valid YAML, an unknown field, a private key where a public key belongs, a duplicate rule, an API key naming an unknown agent), or an approval expiry is above the maximum (`approval expiry refused: ...`). Nothing was written. An expiry is refused, never clamped. |
| `2` | `TETHER_ENV` is not set or invalid, or `TETHER_MIGRATOR_DATABASE_URL` is missing. |

Example:

```bash
tether bootstrap apply config/bootstrap/dev.yaml
```

## `tether apikey new`

Issues a workspace API key. It generates the key locally and prints it; it does not touch the database, and it stores nothing. To activate the key, paste the printed bootstrap entry into the workspace's `api_keys` and run `tether bootstrap apply`.

```text
tether apikey new KEY_ID --agent AGENT [--agent AGENT ...]
```

| | |
|---|---|
| Argument | `KEY_ID`: lowercase letters, digits and dashes, starting with a letter or digit, at most 63 characters. |
| `--agent` | Required, repeatable. An agent this key may run. The command prints the list; it does not check that the agents exist. |
| Environment | none. |

Output (illustrative; values differ on every run):

```text
Raw API key (shown once; give it to the calling app, never commit it):
  tak_demo-app_<52 characters of base32>
Bootstrap entry for the workspace's api_keys:
  - key_id: demo-app
    salt: <32 hex characters>
    secret_hash: <64 hex characters>
    agents: [incident-remediator]
```

The raw key has the form `tak_<key_id>_<secret>`. Tether keeps only the salt and a SHA-256 hash of the salt and secret. Hand the raw key to the calling application and never commit it; the hash and salt are safe to keep in the bootstrap file. A key is bound to one workspace, the one whose bootstrap section lists it.

| Exit | Meaning |
|---|---|
| `0` | Issued. |
| `2` | `KEY_ID` is not valid. Nothing is printed to standard output. |

## `tether audit verify`

Recomputes one run's audit chain from the database and checks the persisted head. It is read-only. It reads the head and the events in one consistent snapshot, so a run being written to cannot look torn. What the chain proves, and what it does not, is in [audit](../governance/audit.md).

```text
tether audit verify --workspace WORKSPACE --run RUN_ID
```

| Option | Meaning |
|---|---|
| `--workspace` | The workspace that owns the run. Required. A run is always looked up inside one workspace. |
| `--run` | The run ID, a UUID. Required. |

| | |
|---|---|
| Environment | `TETHER_ENV`, `TETHER_DATABASE_URL` (the application role), and the audit key family: `TETHER_AUDIT_KEY_<ID>` for every key id that signed one of the run's events. |

Output carries identifiers, counts and codes only: never payloads, MACs, keys or connection details.

A valid chain (illustrative values):

```text
VALID
workspace: ops-demo
run: 5b0c2f0e-0000-4000-8000-000000000001
events: 12
last_seq: 12
head: seq=12 consistent
mac_versions: 2
```

An invalid chain prints `INVALID`, the identity lines, then `reason:` (a stable code), `failed_seq:` when one event is to blame, and `events:`. Reason codes: `sequence_mismatch` (an event is not at the next contiguous number), `prev_mac_mismatch` (it does not link to its predecessor), `unsupported_mac_version`, `mac_mismatch` (an event's recomputed MAC differs from the stored one), `head_mismatch` (the persisted head disagrees with the events).

`mac_versions` lists the MAC versions present. Version 1 events were written before timestamps were covered; version 2 covers `created_at`. A chain may contain both.

| Exit | Output | Meaning |
|---|---|---|
| `0` | `VALID` | The chain recomputes and the head agrees. |
| `1` | `INVALID` | The stored data fail verification. |
| `2` | message on standard error | Usage or configuration error: `--run` is not a UUID, `--workspace` is empty, `TETHER_DATABASE_URL` is missing, or `TETHER_ENV` is not set. Nothing was verified. |
| `3` | message on standard error | Operational failure: the database or an audit key was unavailable. Nothing can be said about the chain, and this is never reported as `INVALID`. |
| `4` | `NOT_FOUND` | There are no events for that run in that workspace (unknown run, or another workspace's run). |

Verification catches a modified or reordered event and a head that disagrees with the events. It does not detect deleting a run's tail together with rewriting its head, or deleting the whole run.

Example inside the development stack:

```bash
docker compose -f deploy/compose/docker-compose.yml exec api \
  tether audit verify --workspace ops-demo --run 5b0c2f0e-0000-4000-8000-000000000001
```

## Process entry points

These are the commands the container runs. In Compose they are the `command` of the `api` and `worker` services.

### `tether-api`

Runs the HTTP API with uvicorn on `TETHER_API_HOST` and `TETHER_API_PORT` (default `0.0.0.0:8080`). It needs `TETHER_ENV`, `TETHER_DATABASE_URL`, the audit and call-binding key families, and normally `TETHER_BOOTSTRAP_FILE`, which supplies issuers, API keys and policy. Telemetry is set up first and flushed on shutdown; if it cannot start, the process runs without it. If the settings are invalid, it prints the names of the offending variables (never their values) and exits `2`. The route list is in the [API reference](api.md).

### `tether-worker`

Runs the worker: it leases jobs, drives runs and executes tools. It takes the same variables as the API, and at startup it also loads every workload named by an agent in the bootstrap file and builds the configured model provider. It **refuses to start** when a configured workload is missing or broken, when a policy allows a high-risk tool, when an approval expiry is above the maximum, or when the provider is misconfigured (for example, `openai` without `TETHER_OPENAI_MODEL`). Invalid settings exit `2`. It stops cleanly on `SIGINT` and `SIGTERM`. While it can reach the database it refreshes a heartbeat file once per poll, between jobs.

### `tether-worker-healthcheck`

A container healthcheck. It exits `0` if the worker's heartbeat file (`TETHER_WORKER_HEARTBEAT_FILE`) was updated within `TETHER_WORKER_HEARTBEAT_MAX_AGE_SECONDS` (default 30 seconds), and `1` otherwise. It loads the same settings as the worker, so it needs the same environment, and exits `2` if they are invalid. The file is touched only between jobs and only while the worker can reach the database, so a worker that has lost its database reports unhealthy, and so does a healthy worker whose current job has run longer than the max age ([runbooks](../operations/runbooks.md#a-worker-that-will-not-start)).

## Related

- [Configuration](../operations/configuration.md)
- [Deployment](../operations/deployment.md)
- [Runbooks](../operations/runbooks.md)
- [Audit](../governance/audit.md)
- [HTTP API reference](api.md)
