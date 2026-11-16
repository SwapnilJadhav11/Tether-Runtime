# Production configuration and dev/test separation

> **Status:** required V1 documentation (direction record TDR-020; owner decision D-12)
> **Baseline:** commit `ec35254`. Several controls described here are **required, not yet implemented**; §4 lists the gap for each. §6 (migration rollout and downgrade rules) was added with W2.4 on 2026-11-14. The explicit-environment and scripted-provider controls (§3; §4 items 1 and 2) are implemented by W7.1 and the production image variant (§4 items 3 to 5, §4.1) by W7.2. The built-image inspection has not been run on the development host (no Docker build in the lane's environment): it runs in CI's integration job.

## 1. Principle

Production operation must fail closed. A production process never silently selects an environment, never accepts a caller-stated identity, never loads a test provider or scenario file, and never ships demo components. Anything dev- or test-only is available only when the operator has explicitly asked for a `dev` or `test` environment.

## 2. Required production configuration

| Setting | Required value in production | Purpose |
|---|---|---|
| `TETHER_ENV` | `prod`, **explicit** | Selects the production startup path. **D-12:** absent or invalid → `tether-api`, `tether-worker`, `tether-worker-healthcheck` and every database-connecting `tether` command (`migrate`, `bootstrap`, `audit`) refuse to start with exit code 2 and a message that names `TETHER_ENV` and the three valid values, never a credential. `tether --help` and `tether apikey new` need no environment. |
| `TETHER_DATABASE_URL` | `postgresql://tether_app:…@…/tether` | Least-privilege application role (no DDL, no DELETE, append-only audit) |
| `TETHER_MIGRATOR_DATABASE_URL` | `tether_migrator` role, used only by `tether migrate` and `tether bootstrap apply` | Separates schema and configuration writes from runtime |
| `TETHER_BOOTSTRAP_FILE` | path to the reviewed, version-controlled bootstrap YAML | Workspaces, issuers (public keys only), API-key hashes, agents, policy (ADR-0014) |
| `TETHER_AUDIT_ACTIVE_KEY_ID`, `TETHER_AUDIT_KEY_<ID>` | 32-byte random secrets, independent per key ID | Audit HMAC chain (ADR-0015) |
| `TETHER_BINDING_ACTIVE_KEY_ID`, `TETHER_BINDING_KEY_<ID>` | independent from the audit family | Call-binding HMAC (ADR-0010) |
| Provider credentials (OpenAI key) | via the secrets mechanism of the host, never in bootstrap files or images | TDR-004 |
| `TETHER_RUN_MAX_DURATION_SECONDS` | deliberate value (development default 3600) | Run deadline (D-8) |
| `TETHER_APPROVAL_TTL_SECONDS` | deliberate value within the maximum (development default 3600) | Default approval expiry, used when the tool's `require_approval` policy rule sets no `approval_expiry_seconds` (W4.2, D-8) |
| `TETHER_APPROVAL_MAX_TTL_SECONDS` | deliberate value (development default 86400; proposed, awaiting owner confirmation) | Upper bound for the default and for every rule's `approval_expiry_seconds`; a value above it is refused, never clamped, by `tether bootstrap apply` (before anything is written) and at worker startup. Each reads both approval settings from its own environment, so give the CLI and the worker the same values |
| `TETHER_OTEL_ENDPOINT` | collector URL, or unset | Telemetry is best-effort |
| `TETHER_LOG_LEVEL` | `INFO` or `WARNING` | Logs are JSON with scrubbing |

Settings that must **not** be set or must have no effect in production: `TETHER_SCENARIOS_DIR` (scripted provider scenarios), any dev identity body or query parameter.

## 3. Controls that separate development and test from production

| Control | Design | At baseline `ec35254` |
|---|---|---|
| Explicit environment | `TETHER_ENV` must be present and one of `dev`, `test`, `prod` | **Implemented (W7.1).** `Settings.env` and `MigrationSettings.env` have no default; the entrypoints read settings through `load_settings()` and the CLI gates its database commands in one place (`src/tether/cli/main.py`). Tests: `tests/unit/test_T32_explicit_environment.py`. |
| Dev identity shortcut | Only with `TETHER_ENV ∈ {dev, test}` and no `Authorization` header; produces the same `Principal` and passes the same authorization as a real token; `401` in `prod` | Implemented and isolated in `src/tether/api/dev_identity.py`. The environment is now always explicit, so an omitted variable cannot enable it. The module is still part of every build (the route dependencies import it); it is inert unless the environment is `dev` or `test`. |
| Scripted model provider | Loads only with an explicit `dev`/`test`; the production provider is selected by configuration | **Implemented (W7.1).** The provider is chosen by `TETHER_MODEL_PROVIDER` (`src/tether/providers/factory.py`). `ScriptedProvider` accepts only `dev` or `test` (an allow-list: `prod`, unset and misspelt values are all refused) and decides before it reads any scenario file. The factory imports it only for the `scripted` choice, so a build without the module reports a configuration error. `TETHER_MODEL_PROVIDER` still defaults to `scripted`, which `prod` refuses: a production deployment sets `TETHER_MODEL_PROVIDER=openai` explicitly. |
| Scenario files | Dev/test only | **Implemented (W7.2).** The `dev` image target copies them to `/app/scenarios`; the `prod` target contains no scenario directory (the demo workload and `workloads/change_control/scenarios` are deleted before its wheel is built). Static proof: `tests/unit/test_t32_production_image.py`; built-image proof: `tests/integration/test_production_image_built.py`. |
| Demo components | `demo-target-service` is a separate container and must not be in the production stack; the reference workload is a demo client | **Implemented (W7.2).** The `prod` target excludes the demo workload `workloads/incident_remediation` (module and `tether.workloads` entry point), the scripted provider module `src/tether/testing/scripted.py`, `config/` (the dev and sample bootstrap files) and every dev tool. The demo target is a separate image the production stack never builds. What remains in the wheel is the core and `workloads/change_control`. |
| Issuer private keys | Never deployed with Tether; dev minting tool outside the image | Correct: `tools/dev_issuer/.keys/` is git-ignored; `config/bootstrap/dev.yaml` holds public keys and API-key hashes only; bootstrap rejects private key material |
| Secrets in bootstrap | None; salted hashes only | Correct |
| Admin surface | No admin HTTP API; configuration changes are apply + restart | Correct (ADR-0014) |
| Policy version | Processes refuse to start or decide under a stale policy version | Correct (ADR-0009) |

## 4. Required changes before a production claim (D-12)

1. **Done (W7.1).** `TETHER_ENV` is mandatory: no default; invalid or missing → startup error naming the variable.
2. **Done (W7.1).** The model provider is selected by configuration; the scripted provider is constructible only when `TETHER_ENV` is `dev` or `test`.
3. **Done (W7.2).** One Dockerfile, two targets (`prod`, and `dev` as the last and default stage); the production target contains no scenario files and no demo-only workload; Compose builds `target: dev`.
4. **Done (W7.1, W7.2).** `T-32`: unit tests for the startup refusal and the environment binding of the dev shortcut and the scripted provider (`tests/unit/test_T32_explicit_environment.py`); a static check of the Dockerfile and packaging (`tests/unit/test_t32_production_image.py`); a built-image inspection (`tests/integration/test_production_image_built.py`, skipped without a Linux Docker daemon).
5. **Done (W7.2).** The Compose stack is **development only** (`TETHER_ENV=dev` is set explicitly in `deploy/compose/docker-compose.yml`); `deploy/compose/.env.production.example` is the production example, placeholders only.

### 4.1 Building and running the production image

```
docker build --target prod -t tether-runtime:prod .
```

The image sets no `TETHER_ENV`, no bootstrap file and no key: the operator supplies all of them (see `deploy/compose/.env.production.example`) and mounts the reviewed bootstrap file at `TETHER_BOOTSTRAP_FILE`. A plain `docker build .` without `--target` produces the **dev** image.

What the production image still contains, deliberately: the core, including `src/tether/api/dev_identity.py` (the route dependencies import it; it is inert unless the environment is `dev` or `test`, and the production-environment tests prove a request without credentials gets `401`), and `workloads/change_control`. Removing the dev identity module from the build would need a change to the route dependencies and is not part of W7.2.

## 5. Production startup checklist

- [ ] Running the `prod` image target (`docker build --target prod`), not the default (dev) image
- [ ] `TETHER_ENV=prod` set explicitly; `TETHER_MODEL_PROVIDER=openai` and `TETHER_OPENAI_MODEL` set
- [ ] Database roles created by the init script; `tether migrate` and `tether bootstrap apply` run by the migrator role before `api`/`worker`
- [ ] Bootstrap file reviewed: public keys only, API-key hashes only, one policy per workspace
- [ ] Audit and binding keys present for every key ID referenced by non-terminal runs
- [ ] Provider credential present through the host's secret mechanism
- [ ] No `TETHER_SCENARIOS_DIR`, no demo target in the stack
- [ ] `/readyz` ready; worker heartbeat fresh
- [ ] Backup and restore procedure in place (`postgres-recovery.md`)
- [ ] Application and schema versions match; no process older than the schema is running (§6)

## 6. Schema migrations, rollout and rollback (owner-approved constraints, 2026-11-14)

The application and the `tether` schema are deployed as a **compatible pair**. Some migrations are deliberately not compatible with writers from older application versions; for those, a mixed old-writer/new-schema rolling deployment is **not a supported deployment mode**.

### 6.1 Migration `0003` (audit `mac_version`, W2.4)

`0003` adds a required `audit_events.mac_version` with **no persistent database default**: every audit append must state how its MAC is computed. Application processes from before W2.4 do not, so **old processes and schema `0003` are intentionally not writer-compatible**.

Required sequence:

1. Drain and stop every old `api` and `worker` process.
2. Apply the migration (`tether migrate`, migrator role).
3. Start the new `api` and `worker` version.

Do not run old and new writers across this migration at the same time. If an old writer does run against `0003`, its audit append fails and its whole transaction aborts, so the state change it was making does not happen (fail closed). That is a consequence of an incompatible rollout sequence, not a supported mode of operation. W2.4 is not redesigned to let old and new writers coexist.

### 6.2 Downgrade posture

- **Once any version-2 audit event has been written, downgrading to a schema or application version that does not understand `mac_version` is unsupported.** Version-2 events can be verified only by code that knows version 2; dropping the column discards the interpretation needed to verify them.
- `alembic downgrade 0002` (the `0003` downgrade) exists for development and test databases only. **It is not a production rollback procedure** once v2 data exists.
- Production rollback or recovery must always end in a compatible application + schema pair: roll the application forward to a version that understands the schema, or restore a database backup together with the application version that matches it (`postgres-recovery.md` §4). Never pair a database that holds v2 events with an application that predates W2.4.

### 6.3 What `0003` does not change

- Events written before `0003` are version 1: valid as written, never re-MACed, and **their timestamps remain cryptographically uncovered**. W2.4 makes no retroactive protection claim.
- Deletion or truncation of a chain's tail, or of a whole run, remains outside the HMAC guarantee (ADR-0015 rule 8).
- `tether audit verify --workspace <ws> --run <run-id>` recomputes one run's chain from the database (needs `TETHER_DATABASE_URL` and the audit keys in the environment; read-only). Exit codes: `0` VALID, `1` INVALID (stable reason code and first failing `seq`), `2` usage or configuration, `3` operational (database or audit key unavailable; never reported as INVALID), `4` NOT_FOUND (an unknown run and a run in another workspace look identical). It also checks that the persisted head names an existing event with the same MAC, which exposes deleting events while leaving the head alone. It does **not** detect deleting events *and* rewriting the head, or deleting the whole run (ADR-0015 rule 8). Chain validity is also exposed through run inspection.
- TX5 refusals still write no audit event of their own (P-16).
