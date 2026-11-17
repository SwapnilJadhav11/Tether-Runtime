# Deployment

How to take Tether from nothing to a running production-shaped deployment, scale it, upgrade it and reason about its failure modes. It is written for the operator of a single host.

**Scope.** Tether's deployment target is one host running containers (Docker Compose or an equivalent) and one PostgreSQL instance. There is no Kubernetes packaging, no high-availability guidance and no published operating limit. The Compose stack in `deploy/compose/` is the development stack; it is not a production deployment. For the development stack, see [getting started](../development/getting-started.md).

## What runs

One image definition builds two variants (`Dockerfile`, [ADR-0001](../adr/0001-two-process-topology-postgres-only.md)). PostgreSQL is the only stateful dependency: there is no broker and no cache.

| Variant | Build | Contains | Leaves out |
|---|---|---|---|
| `prod` | `docker build --target prod -t tether-runtime:prod .` | The core and the `change_control` workload | The incident-remediation demo workload, every scenario directory, the scripted model provider, `config/` (including the sample bootstrap files) and all development tooling |
| `dev` | `docker build .` (the last stage, so the default) | Everything in `prod`, plus the demo workload, the incident-remediation scenarios (at `/app/scenarios`) and all of `config/` (both sample bootstrap files) | Nothing |

The `prod` image sets no `TETHER_ENV`, no bootstrap file and no key. The operator supplies all three at run time. A plain `docker build .` produces the `dev` image, so always pass `--target prod` for production.

The image holds one set of entry points. You choose the process with the container command:

| Command | Process | Long-running |
|---|---|---|
| `tether-api` | HTTP API, port 8080 (`TETHER_API_PORT`) | yes |
| `tether-worker` | Leases and runs jobs; drives runs | yes |
| `tether-worker-healthcheck` | Exits 0 while the worker's heartbeat file is fresh | no (container health check) |
| `tether migrate` | Applies schema migrations and the LangGraph saver setup | no (one-shot) |
| `tether bootstrap apply <path>` | Writes the bootstrap file to PostgreSQL | no (one-shot) |

Both long-running processes run as an unprivileged user (uid 10001). The API and the workers do not talk to each other: they coordinate only through PostgreSQL. See [architecture overview](../architecture/overview.md).

## Database roles

Two PostgreSQL roles separate schema and configuration writes from runtime.

| Role | Used by | Can |
|---|---|---|
| `tether_migrator` | `tether migrate`, `tether bootstrap apply` | Owns the `tether` and `langgraph` schemas; runs DDL; writes configuration |
| `tether_app` | `tether-api`, `tether-worker`, `tether audit verify` | Reads and writes data; no DDL; no `DELETE` on the `tether` schema tables; audit and access-event tables are append-only |

`deploy/compose/postgres/init.sh` is the development stack's first-boot script, not a production script. It creates one `tether` database owned by the migrator, a `tether` schema and a `langgraph` schema, `CONNECT` and schema `USAGE` for the application role, and default privileges so that tables the migrator creates later are usable by the application role. It also creates a demo database, runs as the PostgreSQL superuser, and its default privileges give `tether_app` `DELETE` and `UPDATE` on the `langgraph` checkpoint schema. Migrations apply narrower grants on the `tether` schema where a table must be append-only; they do not change the `langgraph` grants.

For production, create the `tether_migrator` and `tether_app` roles as in `init.sh`, then take `DELETE` away from `tether_app` on the `langgraph` schema and set no default `DELETE` privilege for it. Check the result with psql's privilege listing (`\dp langgraph.*` and `\ddp`). The development stack uses PostgreSQL 16.

Require TLS to PostgreSQL (for example `?sslmode=verify-full` in the database URLs) unless the link is a trusted local network.

The migrator credential is used only by one-shot commands and never given to the API or a worker. LangGraph checkpoints live in the `langgraph` schema; they are a progress cache, not the record ([ADR-0003](../adr/0003-authoritative-tables-checkpoint-progress-cache.md)).

## First-time setup

Prerequisites: a host with a container runtime, a PostgreSQL instance reachable from it, a reviewed bootstrap file, an OpenAI API key and model name, and the ability to inject secrets into containers without writing them to the repository.

1. **Build the image.** `docker build --target prod -t tether-runtime:prod .`
2. **Prepare PostgreSQL.** Create the `tether_migrator` and `tether_app` roles, the `tether` database and the two schemas as described above.
3. **Generate the keys.** Tether needs two independent key families, each with at least one key: audit-chain keys and call-binding keys. Each key is 32 random bytes in base64url. Generate one per family:

   ```
   python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```

   Key IDs are 1 to 32 characters from `A-Z` and `0-9`. Never reuse material across the two families. See [key lifecycle](key-lifecycle.md).
4. **Write the environment.** Start from `deploy/compose/.env.production.example` (placeholders only) and deliver the real values through your host's secret mechanism, not through a file in the repository. The variables are listed in [configuration](configuration.md). At minimum: `TETHER_ENV=prod`, `TETHER_DATABASE_URL`, `TETHER_MIGRATOR_DATABASE_URL` (migrate and bootstrap steps only), `TETHER_BOOTSTRAP_FILE`, the four key variables, `TETHER_MODEL_PROVIDER=openai`, `TETHER_OPENAI_MODEL` and `TETHER_OPENAI_API_KEY`.
5. **Review and mount the bootstrap file.** It declares workspaces, agents, token issuers (public keys only), API-key hashes and policy. It must contain no private key and no raw API key. Mount it read-only at the path in `TETHER_BOOTSTRAP_FILE` in every container that runs `tether-api`, `tether-worker` or the apply step. See [configuration](configuration.md#the-bootstrap-file).
6. **Migrate.** Run `tether migrate` with `TETHER_ENV` and `TETHER_MIGRATOR_DATABASE_URL` set. It is idempotent and prints `migrations applied`.
7. **Apply the bootstrap file.** Run `tether bootstrap apply /etc/tether/bootstrap.yaml` (use your mount path). It validates the file, then writes it in one transaction as the migrator role and prints `bootstrap applied: <n> workspace(s)`. It also refuses an approval expiry above the maximum before writing anything.
8. **Start the workers, then the API.** Start one or more `tether-worker` containers and one `tether-api` container, all with the application role's `TETHER_DATABASE_URL`. A worker refuses to start if its bootstrap file names a workload that cannot be loaded, if the policy allows a high-risk tool, if an approval expiry exceeds the maximum, or if its model provider is misconfigured. Only settings validation exits with code 2; the other refusals are uncaught errors with a traceback and exit code 1. The API checks none of these, so it can start while the worker refuses (for example with `TETHER_MODEL_PROVIDER=scripted` in `prod`); see [runbooks](runbooks.md#a-worker-that-will-not-start).
9. **Check health.**
   - API liveness: `GET /healthz` returns `{"status":"ok"}` and does not touch the database.
   - API readiness: `GET /readyz` returns `{"status":"ready"}`, or `503` with `{"status":"unavailable"}` if the application role cannot reach the database within `TETHER_READINESS_TIMEOUT_SECONDS`.
   - Worker: run `tether-worker-healthcheck` inside the container. It exits 0 while the heartbeat file (`TETHER_WORKER_HEARTBEAT_FILE`) was touched within `TETHER_WORKER_HEARTBEAT_MAX_AGE_SECONDS`, and 1 otherwise. The worker touches the file only between jobs, once per pass of its loop while the database is reachable. A stale file therefore means the database is unreachable **or** a single job has run longer than the max age (default 30 s), even with a healthy database: a model call can take up to 60 s by default and a tool timeout up to 300 s. Set the max age above the longest single job you expect, or the container will be reported unhealthy during normal work.
10. **Submit a test run** with real credentials and read it back, following [the API reference](../reference/api.md). In `prod` the API serves no OpenAPI schema and no interactive docs.

Both probes are unauthenticated and return no data. `tether-api` binds `0.0.0.0:8080` over plain HTTP by default (`TETHER_API_HOST`, `TETHER_API_PORT`) and Tether does not terminate TLS: bearer tokens and API keys travel in request headers, so terminate TLS in a proxy in front of it and do not expose port 8080 directly.

Before declaring the deployment ready, walk the checklist in [production configuration](production-configuration.md#5-production-startup-checklist). That page also lists what the production image refuses to start without.

## Startup order

Order matters at three points.

| Step | Must be done before | Why |
|---|---|---|
| `tether migrate` | any API or worker start | The processes expect the current schema |
| `tether bootstrap apply` | any worker start, and after every policy change | A worker only decides under the policy version that is active in the database. A worker whose file hashes to a different version decides nothing |
| Keys present | any API or worker start | A missing audit or binding key makes every audit write or call hash fail closed |

The API and the workers can start in either order once these hold. Both load the bootstrap file at start-up and do not re-read it, so a changed file takes effect only after a restart.

## Scaling workers

Run more `tether-worker` containers against the same database. Workers are stateless and compete for jobs with `FOR UPDATE SKIP LOCKED`. A run is advanced by at most one worker at a time, enforced by a per-run advisory lock and a fencing token, so extra workers add throughput across runs, never inside one. See [execution and recovery](../architecture/execution-and-recovery.md).

There is no autoscaler and no published limit for the number of workers, runs or jobs. The validation ran three worker processes on one host; see [validation](../validation.md). Size by observing queue depth and pickup latency ([observability](observability.md#what-to-alert-on)).

To drain a worker, send `SIGTERM` (or `SIGINT`). It finishes the job it is processing, then exits. Set the container stop grace period above your longest job: the container runtime's default (10 s in Docker) kills the process mid-job. A kill is safe, because the lease expires and the ledger rules apply, but it delays the job by up to a lease. A worker that dies mid-job is not a data problem: the job's lease expires and another worker takes it over, and the ledger rules decide what may be re-sent.

## Upgrades

Some migrations are deliberately incompatible with writers from older versions. Old and new application versions must not write to the same database at the same time. Use this sequence for every upgrade:

1. Drain and stop every old `tether-api` and `tether-worker` process.
2. Run `tether migrate` (migrator role).
3. Run `tether bootstrap apply <file>` if the new version changed the policy evaluator or you changed the file. See below.
4. Start the new `tether-worker` and `tether-api` containers.

Why not a rolling upgrade. Migration `0003` adds a required `audit_events.mac_version` with no database default, so an older process's audit append fails and its whole transaction aborts. Migration `0005` adds the `verify_outcome` job kind and verified resolutions that an older worker neither leases nor understands. Failing closed protects the data, but a mixed fleet is not a supported mode. The full constraints, kept verbatim, are in [production configuration §6](production-configuration.md#6-schema-migrations-rollout-and-rollback).

**After a policy or evaluator change, re-apply the bootstrap file.** The policy version is the SHA-256 of the canonical policy section together with the evaluator version. Deploying a release with a new evaluator version, or editing `policy`, changes the hash. Until `tether bootstrap apply` records and activates the new version, workers decide nothing under it, and the approval and reconciliation routes return `503`. So: apply, then restart workers and the API so they load the same file.

**Rollback.** There is no production downgrade. Once version-2 audit events exist, a schema or application version that does not understand `mac_version` cannot verify them, and the migrations' downgrade functions are for development and test databases only. Recovery always ends in a compatible application and schema pair: roll forward to a version that understands the schema, or restore a database backup together with the matching application version. See [PostgreSQL recovery](postgres-recovery.md).

## When PostgreSQL is unavailable

While PostgreSQL is down, nothing progresses and nothing is lost.

- The API fails requests, and `/readyz` returns `503`. Every submission, decision, reconciliation and cancellation is a single transaction that either committed or did not. Retrying is safe for decisions and cancellation: a repeated approval or rejection is `409`, a repeated reconciliation with the same outcome is a no-op `200`, and cancellation is idempotent. Submission is **not** idempotent: it creates a new run id each time, so a client that lost the response must check whether its run exists before submitting again, or it may create a second run.
- Workers cannot lease, extend leases or commit. A handler that cannot beat its lease for a full lease period is aborted as if the worker had died. The worker keeps polling but stops touching its heartbeat file, so its health check fails.
- A tool call that was in flight cannot record its outcome. When the database returns, the ledger entry is still `dispatched` and is handled by the ledger rules: an idempotent write is re-sent with the same key within its budget; a non-idempotent write or an exhausted budget becomes `indeterminate` and goes to automatic verification, then to a person. See [unknown outcomes](../governance/unknown-outcomes.md).
- Timers (approval expiry, run deadline) fire late, but both are also enforced against the database clock at decision time.

Backup, restore and failover are in [PostgreSQL recovery](postgres-recovery.md). No restore drill has been performed and no recovery objective is set.

## Related

- [Configuration](configuration.md): every setting and the bootstrap file
- [Production configuration](production-configuration.md): fail-closed rules and the startup checklist
- [Observability](observability.md): logs, traces and metrics
- [Runbooks](runbooks.md): procedures and troubleshooting
- [PostgreSQL recovery](postgres-recovery.md)
