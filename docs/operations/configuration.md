# Configuration

The complete reference for configuring Tether: every environment variable, the bootstrap file, and what differs between `dev`, `test` and `prod`. It is written for operators and for engineers who need to look up one setting.

Tether has no admin HTTP API ([ADR-0014](../adr/0014-bootstrap-config-no-admin-api.md)). Two things configure it: environment variables, read at process start, and one reviewed YAML file, the bootstrap file, applied to PostgreSQL with `tether bootstrap apply`. Nothing is reloaded while a process runs: change the environment or the file, then restart.

A missing or invalid required setting stops the process with exit code 2 and a message that names the variable and never its value.

## Environment and identity

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TETHER_ENV` | none | Yes, always | `dev`, `test` or `prod`. There is deliberately no default. Absent or invalid, `tether-api`, `tether-worker`, `tether-worker-healthcheck` and the database commands `tether migrate`, `tether bootstrap` and `tether audit` exit with code 2. `tether --help` and `tether apikey new` need no environment. Also reported as `deployment.environment.name` on telemetry. |
| `TETHER_BOOTSTRAP_FILE` | unset | In practice, yes | Path of the bootstrap file inside the container. Read by `tether-api` and `tether-worker` at start. If unset, both run with an empty configuration: no workspace is known, every approval and reconciliation route returns `404`, and every run fails with `configuration_unavailable`. |

## Database

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TETHER_DATABASE_URL` | none | Yes (api, worker, `tether audit verify`) | PostgreSQL URL of the least-privilege application role (`tether_app`). Treated as a secret: never logged, and scrubbed from logs. |
| `TETHER_MIGRATOR_DATABASE_URL` | none | Yes (`tether migrate`, `tether bootstrap apply` only) | PostgreSQL URL of the migrator role (`tether_migrator`). Give it to one-shot jobs only, never to the API or a worker. |
| `TETHER_READINESS_TIMEOUT_SECONDS` | `2.0` | No | How long `/readyz` and the worker's reachability check wait for a database connection. |

## API

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TETHER_API_HOST` | `0.0.0.0` | No | Interface `tether-api` listens on. |
| `TETHER_API_PORT` | `8080` | No | Port `tether-api` listens on. |

## Model provider

Provider behaviour and credential handling are in [model providers](model-providers.md).

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TETHER_MODEL_PROVIDER` | `scripted` | Yes in `prod` | `scripted` or `openai`. The scripted provider loads only when `TETHER_ENV` is `dev` or `test`, so `prod` must set `openai`. Anything else fails start-up. |
| `TETHER_OPENAI_API_KEY` | none | Yes with `openai` | The provider credential. Not a settings field: it is read from the environment through the secrets provider, so it cannot appear in a settings dump, and it is scrubbed from logs. |
| `TETHER_OPENAI_MODEL` | none | Yes with `openai` | Model name. There is no default: Tether never spends on a model nobody chose. |
| `TETHER_OPENAI_BASE_URL` | `https://api.openai.com/v1` | No | For a proxy or an OpenAI-compatible gateway. |
| `TETHER_OPENAI_TIMEOUT_SECONDS` | `60.0` | No | Upper bound on one model call. Must be positive. |
| `TETHER_PROVIDER_MAX_ATTEMPTS` | `3` | No | Provider attempts per model turn, retries included. Must be positive. |
| `TETHER_SCENARIOS_DIR` | `scenarios` | `dev`/`test` with `scripted` | Directory of scenario files for the scripted provider. Must not be set in `prod`; the `prod` image contains none. |

## Run limits

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TETHER_RUN_MAX_DURATION_SECONDS` | `3600` | No (set deliberately) | Each run's deadline is its submission time plus this. Read by the API at submission. At the deadline the run is stopped and ends `timed_out`. |
| `TETHER_MAX_MODEL_TURNS` | `8` | No | Model turns per run. Must be positive. |
| `TETHER_MAX_REPAIRS` | `3` | No | Invalid proposals (unknown tool, invalid or malformed arguments) a run tolerates. One more ends the run `failed` with `repair_budget_exhausted`. Must be positive. |
| `TETHER_MAX_DENIALS` | `3` | No | Policy denials a run tolerates. One more ends the run `failed` with `denial_budget_exhausted`. Must be positive. |

## Approvals

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TETHER_APPROVAL_TTL_SECONDS` | `3600` | No (set deliberately) | How long an approval request stays decidable when its tool's rule sets no `approval_expiry_seconds`. Must be positive. |
| `TETHER_APPROVAL_MAX_TTL_SECONDS` | `86400` | No | The largest expiry any rule or the default may set. A larger value is refused, never clamped, by `tether bootstrap apply` (before it writes anything) and at worker start-up. Give the CLI and the workers the same two values. |

## Worker and queue

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TETHER_WORKER_POLL_INTERVAL_SECONDS` | `1.0` | No | How often an idle worker polls for due jobs. |
| `TETHER_WORKER_LEASE_SECONDS` | `60.0` | No | How long a leased job stays invisible to other workers before it can be reclaimed. Must be positive. |
| `TETHER_WORKER_LEASE_HEARTBEAT_SECONDS` | unset (lease / 3) | No | How often a worker extends the lease of the job it is processing. Must be positive and below the lease, or start-up fails. |
| `TETHER_WORKER_HEARTBEAT_FILE` | `tether-worker.heartbeat` in the system temporary directory | No | File the worker touches each poll while the database is reachable. |
| `TETHER_WORKER_HEARTBEAT_MAX_AGE_SECONDS` | `30.0` | No | `tether-worker-healthcheck` exits 1 when the file is older than this. |

Retry and delivery limits are not settings. A job is delivered at most 8 times for `advance_run`, 5 for `execute_tool` and `verify_outcome`, and 10 for the two timers. See [execution and recovery](../architecture/execution-and-recovery.md).

## Telemetry

Telemetry is best-effort; see [observability](observability.md).

| Variable | Default | Required | Meaning |
|---|---|---|---|
| `TETHER_LOG_LEVEL` | `INFO` | No | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Use `INFO` or `WARNING` in production. |
| `TETHER_OTEL_ENDPOINT` | unset | No | OTLP/HTTP base URL of a collector, for example `http://otel-collector:4318`. Traces go to `<base>/v1/traces` and metrics to `<base>/v1/metrics`. Unset, nothing is exported. |
| `TETHER_OTEL_EXPORT_TIMEOUT_SECONDS` | `2.0` | No | Upper bound for one export attempt, retries included. A dead collector never blocks a request or job. |
| `TETHER_OTEL_METRIC_INTERVAL_SECONDS` | `15.0` | No | How often metrics are exported, and how often a worker samples queue depth. |

## Keys

Tether uses two independent families of 32-byte random keys. Each key is base64url (unpadded, or with its padding). Key IDs are 1 to 32 characters from `A-Z` and `0-9`. A key must never serve both families. Rotation is manual; see [key lifecycle](key-lifecycle.md).

| Variable | Required | Meaning |
|---|---|---|
| `TETHER_AUDIT_ACTIVE_KEY_ID` | Yes (api, worker, `tether audit verify`) | The audit key ID new events are MACed with. |
| `TETHER_AUDIT_KEY_<ID>` | Yes, for every ID in use | Audit key material for that ID. Keep every key that signed an event you still want to verify. |
| `TETHER_BINDING_ACTIVE_KEY_ID` | Yes (api, worker) | The binding key ID new runs pin. |
| `TETHER_BINDING_KEY_<ID>` | Yes, for every ID a non-terminal run pins | Binding key material for that ID. A run whose pinned key is missing fails closed. |

Keys are read from the process environment. Deliver them through your host's secret mechanism.

## Workload variables

Read by the workload's own tools, not by Tether's settings.

| Variable | Default | Workload | Meaning |
|---|---|---|---|
| `CHANGE_CONTROL_GITHUB_REPO` | none | change control | Target repository as `owner/name`. |
| `CHANGE_CONTROL_GITHUB_TOKEN` | none | change control | GitHub credential. Treated as a secret and never logged. |
| `CHANGE_CONTROL_GITHUB_API_URL` | GitHub's public API | change control | For GitHub Enterprise or tests. |
| `DEMO_TARGET_URL` | `http://demo-target:8000` | incident remediation (`dev` image only) | Base URL of the demo target service. |

## Development stack variables

These are read only by `deploy/compose/docker-compose.yml` and the PostgreSQL first-boot script, not by Tether processes.

| Variable | Meaning |
|---|---|
| `TETHER_POSTGRES_PASSWORD` | PostgreSQL superuser password, used only on first boot of the data volume. |
| `TETHER_MIGRATOR_DB_PASSWORD` | Password the init script gives `tether_migrator`. |
| `TETHER_APP_DB_PASSWORD` | Password the init script gives `tether_app`. |
| `TETHER_OLLAMA_MODEL` | Model name for the stack's optional `ollama-pull` service (`live` profile). Tether has no Ollama provider; the variable is unused by Tether itself. |

Use URL-safe passwords (letters, digits, `-` and `_`): the stack embeds them in database URLs. The stack passes `TETHER_MODEL_PROVIDER`, `TETHER_OPENAI_MODEL` and `TETHER_OPENAI_API_KEY` through from `.env`. It does not forward the two approval variables, so their defaults apply there.

## The bootstrap file

The bootstrap file declares everything platform-level: workspaces, the agents each one runs, the token issuers it trusts, the API keys it accepts, and its policy. It is YAML, version-controlled and reviewed like code. It holds no secret: issuers carry public keys, API keys carry a salt and a hash. Unknown fields are rejected.

| Section | Holds |
|---|---|
| `workspaces[].id` | The workspace identifier. |
| `agents[]` | `id` and `workload` (the name of an installed workload). |
| `issuers[]` | `id`, `iss`, `audiences`, `algorithm`, `public_keys`. Who may sign tokens, for which routes. |
| `api_keys[]` | `key_id`, `salt`, `secret_hash`, `agents`. Which calling applications may submit runs, and for which agents. |
| `policy` | `rules`, `reconciliation`, `role_permissions`, `agent_permissions`. See [policy](../governance/policy.md). |

Field rules:

| Field | Rule |
|---|---|
| `issuers[].iss` | Unique within the workspace. The token's `iss` must equal it. |
| `issuers[].audiences` | One or more of `tether-tasks`, `tether-approvals`, `tether-ops`. A token's audience must also match its route. |
| `issuers[].algorithm` | `EdDSA` (default) or `RS256`. Every key must match it. |
| `issuers[].public_keys` | Map of `kid` to PEM public key. Private key material is rejected. Tokens name their key in the `kid` header. |
| `api_keys[].key_id` | Lower-case letters, digits and `-`, up to 63 characters. Unique, and owned by one workspace. |
| `api_keys[].salt` | 32 lower-case hex characters. |
| `api_keys[].secret_hash` | 64 lower-case hex characters (SHA-256). |
| `api_keys[].agents` | Agents declared in the same workspace. |
| `policy.rules[]` | One rule per tool at most. A tool with no rule is denied. `decision` is `allow`, `deny` or `require_approval`. |
| `policy.rules[].approver_roles` | Required on `require_approval`, forbidden otherwise. |
| `policy.rules[].approval_expiry_seconds` | Only on `require_approval`. Positive, and not above `TETHER_APPROVAL_MAX_TTL_SECONDS`. |
| `policy.reconciliation.reconciler_roles` | Roles that may resolve an `indeterminate` entry. Empty means nobody. |
| `policy.role_permissions` | Role to list of `resource:action` permissions, granted to a requester holding the role. |
| `policy.agent_permissions` | Agent to list of permissions. An agent with no entry has none, which is a deny. Agents must be declared. |

At worker start-up Tether additionally refuses a policy that sets `allow` on a tool the workload declares as high risk. High-risk tools must use `require_approval`.

### An annotated example

Synthetic values throughout. The shape follows `config/bootstrap/change-control.yaml`; replace every placeholder before use. A file with placeholder keys, salts or hashes does not pass validation.

```yaml
workspaces:
  - id: ws-example
    agents:
      - id: change-controller           # the agent a run executes
        workload: change_control        # an installed workload
    issuers:                            # one issuer per route audience
      - id: tasks
        iss: https://issuer.example.com/ws-example/tasks
        audiences: [tether-tasks]
        algorithm: EdDSA
        public_keys:
          tasks-1: |
            -----BEGIN PUBLIC KEY-----
            <base64 of the issuer's Ed25519 public key>
            -----END PUBLIC KEY-----
      - id: approvals
        iss: https://issuer.example.com/ws-example/approvals
        audiences: [tether-approvals]
        public_keys:
          approvals-1: |
            -----BEGIN PUBLIC KEY-----
            <base64 of the issuer's Ed25519 public key>
            -----END PUBLIC KEY-----
      - id: ops
        iss: https://issuer.example.com/ws-example/ops
        audiences: [tether-ops]
        public_keys:
          ops-1: |
            -----BEGIN PUBLIC KEY-----
            <base64 of the issuer's Ed25519 public key>
            -----END PUBLIC KEY-----
    api_keys:                           # from `tether apikey new`
      - key_id: change-app
        salt: <32 hex characters>
        secret_hash: <64 hex characters>
        agents: [change-controller]
    policy:
      rules:
        - tool: get_issue
          decision: allow
        - tool: create_change_issue
          decision: require_approval
          approver_roles: [approver]
          approval_expiry_seconds: 900  # optional; defaults to TETHER_APPROVAL_TTL_SECONDS
      reconciliation:
        reconciler_roles: [reconciler]
      role_permissions:                 # what a requester's roles grant
        operator: [issue:read, issue:create]
      agent_permissions:                # what the agent may do at most
        change-controller: [issue:read, issue:create]
```

A tool call is allowed only if the requester's permissions and the agent's permissions both cover the tool's required permissions. The approver and reconciler roles grant no tool permissions.

### Issuing an API key

```
tether apikey new change-app --agent change-controller
```

It prints the raw key once (`tak_<key_id>_<secret>`), then a `key_id`, `salt`, `secret_hash` and `agents` entry to paste under the workspace's `api_keys`. Tether stores only the hash. The command needs no environment and no database. Repeat `--agent` to bind several agents. Hand the raw key to the calling application and never commit it.

### How applying works

```
tether bootstrap apply /etc/tether/bootstrap.yaml
```

It runs as the migrator role and needs `TETHER_ENV` and `TETHER_MIGRATOR_DATABASE_URL`. It validates the file, checks approval expiries against the limits in its own environment, and writes everything in one transaction: all or nothing. A validation failure exits 1 with `invalid bootstrap file: ...` or `approval expiry refused: ...` and writes nothing. Re-applying an unchanged file changes nothing.

For each workspace in the file, applying:

- creates the workspace and its agents if absent;
- makes the workspace's issuers and API keys exactly what the file says, which **removes** any issuer or API key that is no longer in the file (that is how a credential is revoked) and replaces each API key's agent bindings;
- records the policy version and makes it the only active one. The version is the SHA-256 of the canonical `policy` section together with the evaluator version, so editing `policy`, or upgrading to a release with a new evaluator version, produces a new version.

It does **not** remove workspaces or agents, does not touch a workspace missing from the file, and does not delete earlier policy versions (it only deactivates them).

A worker or API process decides only under the policy version that is active in the database. If the version computed from its loaded file differs, it decides nothing: approval, reconciliation and approval-list routes return `503` with `policy is reloading`, and workers hold runs without deciding. The fix is to apply the file and restart the processes so both agree. See [runbooks](runbooks.md#503-from-approval-or-reconciliation-routes).

## What differs between environments

| Behaviour | `dev` | `test` | `prod` |
|---|---|---|---|
| `TETHER_ENV` | explicit | explicit | explicit |
| Scripted model provider | allowed | allowed | refused |
| Request identity stated in the request body or query (`dev_identity`, `dev_workspace_id`) | accepted when no `Authorization` header is sent | same | never; such a request is `401` |
| OpenAPI schema at `/openapi.json` | served | served | not served |
| Interactive API docs | not served | not served | not served |
| Scenario directory | present in the `dev` image | n/a | absent from the `prod` image |
| Demo workload | in the `dev` image | n/a | not in the `prod` image |
| Real credentials (JWT, API key) | always honoured | always honoured | required |

The stated-identity shortcut lives in `src/tether/api/dev_identity.py`. It ships in every image but is inert outside `dev` and `test`, and a request that carries an `Authorization` header is always authenticated with real credentials. Read-only approval and operations routes never accept it. See [authentication and authorization](../security/authentication-and-authorization.md).

## Related

- [Deployment](deployment.md)
- [Production configuration](production-configuration.md): the fail-closed rules and startup checklist
- [Model providers](model-providers.md)
- [Observability](observability.md)
- [Key lifecycle](key-lifecycle.md)
