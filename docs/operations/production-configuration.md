# Production configuration and dev/test separation

The fail-closed rules for running Tether in production, the controls that keep development and test conveniences out of it, a startup checklist, and the rules for schema migrations, rollout and rollback. For the procedure of setting a deployment up, see [deployment](deployment.md); for every setting, see [configuration](configuration.md).

## 1. Principle

Production operation must fail closed. A production process never silently selects an environment, never accepts a caller-stated identity, never loads a test provider or scenario file, and never ships demo components. Anything dev- or test-only is available only when the operator has explicitly asked for a `dev` or `test` environment.

## 2. Required production configuration

| Setting | Required value in production | Purpose |
|---|---|---|
| `TETHER_ENV` | `prod`, **explicit** | Selects the production startup path. Absent or invalid → `tether-api`, `tether-worker`, `tether-worker-healthcheck` and every database-connecting `tether` command (`migrate`, `bootstrap`, `audit`) refuse to start with exit code 2 and a message that names `TETHER_ENV` and the three valid values, never a credential. `tether --help` and `tether apikey new` need no environment. |
| `TETHER_DATABASE_URL` | `postgresql://tether_app:…@…/tether` | Least-privilege application role: no DDL, no `DELETE` on the `tether` schema tables, append-only audit. The development init script's default privileges also give it `DELETE` and `UPDATE` on the `langgraph` checkpoint schema; remove `DELETE` there in production. Require TLS to PostgreSQL (for example `?sslmode=verify-full`) unless the link is a trusted local network |
| `TETHER_MIGRATOR_DATABASE_URL` | `tether_migrator` role, used only by `tether migrate` and `tether bootstrap apply` | Separates schema and configuration writes from runtime |
| `TETHER_BOOTSTRAP_FILE` | path to the reviewed, version-controlled bootstrap YAML | Workspaces, issuers (public keys only), API-key hashes, agents, policy ([ADR-0014](../adr/0014-bootstrap-config-no-admin-api.md)). Not enforced at startup: without it the API and worker run with an empty configuration, and the human routes return `404` |
| `TETHER_AUDIT_ACTIVE_KEY_ID`, `TETHER_AUDIT_KEY_<ID>` | 32-byte random secrets, independent per key ID | Audit HMAC chain ([ADR-0015](../adr/0015-per-run-hmac-audit-and-authoritative-reconstruction.md)) |
| `TETHER_BINDING_ACTIVE_KEY_ID`, `TETHER_BINDING_KEY_<ID>` | independent from the audit family | Call-binding HMAC ([ADR-0010](../adr/0010-keyed-canonical-call-hash.md)) |
| `TETHER_MODEL_PROVIDER`, `TETHER_OPENAI_MODEL`, `TETHER_OPENAI_API_KEY` | `openai`, a model name, the provider key | The scripted provider is refused in production. The key comes from the host's secrets mechanism, never from bootstrap files or images |
| `TETHER_RUN_MAX_DURATION_SECONDS` | deliberate value (default 3600) | Run deadline |
| `TETHER_APPROVAL_TTL_SECONDS` | deliberate value within the maximum (default 3600) | Default approval expiry, used when the tool's `require_approval` policy rule sets no `approval_expiry_seconds` |
| `TETHER_APPROVAL_MAX_TTL_SECONDS` | deliberate value (default 86400) | Upper bound for the default and for every rule's `approval_expiry_seconds`; a value above it is refused, never clamped, by `tether bootstrap apply` (before anything is written) and at worker startup. Each reads both approval settings from its own environment, so give the CLI and the worker the same values |
| `TETHER_OTEL_ENDPOINT` | collector URL, or unset | Telemetry is best-effort ([observability](observability.md)) |
| `TETHER_LOG_LEVEL` | `INFO` or `WARNING` | Logs are JSON with scrubbing |

Leave `TETHER_SCENARIOS_DIR` (scripted provider scenarios) unset in production, and send no dev identity body or query parameter. Neither is enforced at startup: the `prod` image holds no scenarios, the OpenAI provider ignores the variable, and the identity shortcut is `401` in `prod`. Treat the variable as a checklist item.

The API listens on `0.0.0.0:8080` over plain HTTP. Terminate TLS in front of it.

## 3. Controls that separate development and test from production

| Control | Design | Implementation |
|---|---|---|
| Explicit environment | `TETHER_ENV` must be present and one of `dev`, `test`, `prod` | `Settings.env` and `MigrationSettings.env` have no default; the entrypoints read settings through `load_settings()` and the CLI gates its database commands in one place (`src/tether/cli/main.py`). Tests: `tests/unit/test_T32_explicit_environment.py`. |
| Dev identity shortcut | Only with `TETHER_ENV ∈ {dev, test}` and no `Authorization` header; produces the same `Principal` and passes the same authorization as a real token; `401` in `prod` | Isolated in `src/tether/api/dev_identity.py`. Because the environment is always explicit, an omitted variable cannot enable it. The module is part of every build (the route dependencies import it); it is inert unless the environment is `dev` or `test`. |
| Scripted model provider | Loads only with an explicit `dev`/`test`; the production provider is selected by configuration | The provider is chosen by `TETHER_MODEL_PROVIDER` (`src/tether/providers/factory.py`). `ScriptedProvider` accepts only `dev` or `test` (an allow-list: `prod`, unset and misspelt values are all refused) and decides before it reads any scenario file. The factory imports it only for the `scripted` choice, so a build without the module reports a configuration error. `TETHER_MODEL_PROVIDER` defaults to `scripted`, which `prod` refuses: a production deployment sets `TETHER_MODEL_PROVIDER=openai` explicitly. |
| Scenario files | Dev/test only | The `dev` image target copies them to `/app/scenarios`; the `prod` target contains no scenario directory (the demo workload and `workloads/change_control/scenarios` are deleted before its wheel is built). Static proof: `tests/unit/test_t32_production_image.py`; built-image proof: `tests/integration/test_production_image_built.py`. |
| Demo components | `demo-target-service` is a separate container and must not be in the production stack; the reference workload is a demo client | The `prod` target excludes the demo workload `workloads/incident_remediation` (module and `tether.workloads` entry point), the scripted provider module `src/tether/testing/scripted.py`, `config/` (the dev and sample bootstrap files) and every dev tool. The demo target is a separate image the production stack never builds. What remains in the wheel is the core and `workloads/change_control`. |
| OpenAPI schema | No unauthenticated schema in production | In `prod` the api serves no `/openapi.json` and no interactive docs; `dev` and `test` serve the schema. |
| Issuer private keys | Never deployed with Tether; dev minting tool outside the image | `tools/dev_issuer/.keys/` is git-ignored; `config/bootstrap/dev.yaml` holds public keys and API-key hashes only; bootstrap rejects private key material |
| Secrets in bootstrap | None; salted hashes only | Enforced by bootstrap validation |
| Admin surface | No admin HTTP API; configuration changes are apply + restart | [ADR-0014](../adr/0014-bootstrap-config-no-admin-api.md) |
| Policy version | Processes decide only under the active policy version | A process whose loaded policy differs from the version active in the database decides nothing: the approval and reconciliation routes return `503` and workers hold their runs ([ADR-0009](../adr/0009-policy-engine-current-policy.md); [runbooks](runbooks.md#503-from-approval-or-reconciliation-routes)) |

## 4. Evidence and the production image

The controls above are checked by:

- `tests/unit/test_T32_explicit_environment.py`: the startup refusal and the environment binding of the dev shortcut and the scripted provider;
- `tests/unit/test_t32_production_image.py`: a static check of the Dockerfile and packaging;
- `tests/integration/test_production_image_built.py`: an inspection of a built `prod` image (skipped without a Linux Docker daemon).

The Compose stack in `deploy/compose/` is **development only**: it builds the `dev` target and sets `TETHER_ENV=dev` explicitly. `deploy/compose/.env.production.example` is the production example, placeholders only.

### 4.1 Building and running the production image

```
docker build --target prod -t tether-runtime:prod .
```

The image sets no `TETHER_ENV`, no bootstrap file and no key: the operator supplies all of them (see `deploy/compose/.env.production.example`) and mounts the reviewed bootstrap file at `TETHER_BOOTSTRAP_FILE`. A plain `docker build .` without `--target` produces the **dev** image.

What the production image still contains, deliberately: the core, including `src/tether/api/dev_identity.py` (the route dependencies import it; it is inert unless the environment is `dev` or `test`, and the production-environment tests prove a request without credentials gets `401`), and `workloads/change_control`. Removing the dev identity module from the build would require changing the route dependencies.

## 5. Production startup checklist

- [ ] Running the `prod` image target (`docker build --target prod`), not the default (dev) image
- [ ] `TETHER_ENV=prod` set explicitly; `TETHER_MODEL_PROVIDER=openai` and `TETHER_OPENAI_MODEL` set
- [ ] `tether_migrator` and `tether_app` roles created as in `deploy/compose/postgres/init.sh` (a development script), then `DELETE` taken away from `tether_app` on the `langgraph` schema with no default `DELETE` privilege set, and the result checked with psql's privilege listing; TLS to PostgreSQL required unless the link is a trusted local network
- [ ] `tether migrate` and `tether bootstrap apply` run by the migrator role before `api`/`worker`
- [ ] Bootstrap file reviewed: public keys only, API-key hashes only, one policy per workspace
- [ ] Audit and binding keys present for every key ID referenced by non-terminal runs
- [ ] Provider credential present through the host's secret mechanism
- [ ] No `TETHER_SCENARIOS_DIR`, no demo target in the stack
- [ ] TLS terminated in front of the API; `/readyz` ready; worker heartbeat fresh (and its max age above your longest single job)
- [ ] Backup and restore procedure in place ([PostgreSQL recovery](postgres-recovery.md))
- [ ] Application and schema versions match; no process older than the schema is running (§6)

## 6. Schema migrations, rollout and rollback

The application and the `tether` schema are deployed as a **compatible pair**. Some migrations are deliberately not compatible with writers from older application versions; for those, a mixed old-writer/new-schema rolling deployment is **not a supported deployment mode**.

### 6.1 Migration `0003` (audit `mac_version`)

`0003` adds a required `audit_events.mac_version` with **no persistent database default**: every audit append must state how its MAC is computed. Application processes from before this migration do not, so **old processes and schema `0003` are intentionally not writer-compatible**.

Required sequence:

1. Drain and stop every old `api` and `worker` process.
2. Apply the migration (`tether migrate`, migrator role).
3. Start the new `api` and `worker` version.

Do not run old and new writers across this migration at the same time. If an old writer does run against `0003`, its audit append fails and its whole transaction aborts, so the state change it was making does not happen (fail closed). That is a consequence of an incompatible rollout sequence, not a supported mode of operation. The migration is not designed to let old and new writers coexist.

### 6.2 Downgrade posture

- **Once any version-2 audit event has been written, downgrading to a schema or application version that does not understand `mac_version` is unsupported.** Version-2 events can be verified only by code that knows version 2; dropping the column discards the interpretation needed to verify them.
- `alembic downgrade 0002` (the `0003` downgrade) exists for development and test databases only. **It is not a production rollback procedure** once v2 data exists.
- Production rollback or recovery must always end in a compatible application + schema pair: roll the application forward to a version that understands the schema, or restore a database backup together with the application version that matches it ([PostgreSQL recovery](postgres-recovery.md)). Never pair a database that holds v2 events with an application that predates `0003`.

### 6.3 What `0003` does not change

- Events written before `0003` are version 1: valid as written, never re-MACed, and **their timestamps remain cryptographically uncovered**. There is no retroactive protection.
- Deletion or truncation of a chain's tail, or of a whole run, remains outside the HMAC guarantee ([ADR-0015](../adr/0015-per-run-hmac-audit-and-authoritative-reconstruction.md) rule 8).
- `tether audit verify --workspace <ws> --run <run-id>` recomputes one run's chain from the database (needs `TETHER_DATABASE_URL` and the audit keys in the environment; read-only). Exit codes: `0` VALID, `1` INVALID (stable reason code and first failing `seq`), `2` usage or configuration, `3` operational (database or audit key unavailable; never reported as INVALID), `4` NOT_FOUND (an unknown run and a run in another workspace look identical). It also checks that the persisted head names an existing event with the same MAC, which exposes deleting events while leaving the head alone. It does **not** detect deleting events *and* rewriting the head, or deleting the whole run. Chain validity is also exposed through run inspection. See [runbooks](runbooks.md#verifying-a-runs-audit-chain).
- A refusal at the pre-dispatch re-check writes no audit event of its own.

### 6.4 Migration `0004` (ledger `outcome_class`) and migration `0005` (automatic verification)

- `0004` adds the nullable `ledger_entries.outcome_class`; historical rows stay unclassified.
- `0005` adds `ledger_entries.verification_attempts` (`NOT NULL DEFAULT 0`, existing rows are not rewritten) and `verification_evidence_masked`, the resolutions `verified_applied`/`verified_not_applied` (a verified resolution must name its resolver, `system:verification`, and carry evidence) and the job kind `verify_outcome`. The app role still has no `DELETE`.
- **Rollout.** Use the §6.1 sequence for `0005` too: drain and stop every old `api` and `worker`, apply the migration, start the new version. A worker from before `0005` does not lease `verify_outcome` jobs and its run driver does not know the `verified_*` resolutions, so old and new workers side by side is not a supported mode.
- **Downgrade.** The `0005` downgrade restores the `0004` constraints and is refused by them once a `verified_*` entry or a `verify_outcome` job exists, so that who resolved an entry is never hidden. Like `0003`, it is for development and test databases only, **not a production rollback** (§6.2).
- The policy version also covers the evaluator version (`builtin-2`) and each rule's `approval_expiry_seconds`. After deploying a release that changes the evaluator, run `tether bootstrap apply` before starting the worker, or it decides nothing under a stale policy version.
