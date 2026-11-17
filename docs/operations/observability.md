# Observability

How to see what Tether is doing: structured logs, traces and metrics, which signals to alert on, and how much to trust them. It is written for operators and for engineers debugging a run.

Telemetry is a convenience. PostgreSQL is the record. A run's rows, its ledger and its [audit chain](../governance/audit.md) are authoritative and are read through the [inspection endpoints](runbooks.md#inspecting-a-run) and `tether audit verify`. Logs, traces and metrics help you find the run to look at. Nothing in Tether reads telemetry back to decide anything.

## Best-effort by design

Both processes run normally without telemetry ([ADR-0015](../adr/0015-per-run-hmac-audit-and-authoritative-reconstruction.md)).

- With `TETHER_OTEL_ENDPOINT` unset, spans and metrics are still created (trace context is stored with runs and jobs), but nothing is exported.
- With an endpoint set, export runs on background threads with a short timeout (`TETHER_OTEL_EXPORT_TIMEOUT_SECONDS`, default 2 s). An unreachable collector costs dropped telemetry, never a blocked request, job or shutdown.
- If telemetry setup itself fails, the process logs a warning and runs without traces and metrics.
- Recording a span, event or metric never raises into the caller.

Expect gaps: a collector outage, a crashed process that did not flush, and spans for work that a crash interrupted. Do not use telemetry as evidence of what a run did. Use the database.

## Logs

Both processes write JSON, one object per line, to standard output. Library records (uvicorn, the exporter, LangGraph) go through the same pipeline. `TETHER_LOG_LEVEL` sets the level (`DEBUG`, `INFO`, `WARNING`, `ERROR`).

Every line carries a timestamp (ISO 8601, UTC), the level, the logger name and the event text. Context is bound where it is known:

| Field | Meaning |
|---|---|
| `run_id`, `workspace_id` | The run and its workspace |
| `job_id`, `job_kind` | The job being processed (worker lines) |
| `tool` | The tool involved |
| `trace_id`, `span_id` | The active trace and span, when there is one |

Use `run_id` to follow one run across the API and every worker, and `trace_id` to jump to its trace.

**What is never logged.** Keys named like a payload or a credential are dropped from every record: `payload`, `arguments`, `args`, `task_input`, `history`, `state`, `output`, `messages`, `body`, `headers`, `authorization`, `api_key`, `token`, `password`, `secret` and similar. Registered secret values (the database URL and password, every audit and binding key, the provider key) are replaced with `[REDACTED]` wherever they appear, including inside exception text. Bearer tokens, JWTs and Tether API keys are replaced by their shape. Tracebacks are rendered as text without local variables. INFO lines from the HTTP client libraries are silenced because request URLs can carry tool arguments. See [secrets and data handling](../security/secrets-and-data-handling.md).

Messages worth knowing:

| Level | Message | Meaning |
|---|---|---|
| warning | `lease lost: handler aborted` | A worker lost a job's lease (field `reason`: `lost` or `unreachable`) and abandoned the delivery |
| warning | `run dead-lettered` | An `advance_run` job exhausted its deliveries; the run was moved to `failed` or held |
| warning | `job dead-lettered` | An `execute_tool` job exhausted its deliveries; the ledger entry's outcome was recorded |
| warning | `tool outcome indeterminate` | A write's outcome is unknown |
| warning | `tool outcome unknown; retry scheduled` | A send will be repeated with the same idempotency key |
| warning | `verification ended; reconciliation required` | Automatic verification gave up; a person must resolve the entry |
| warning | `run held: configuration unavailable` | A worker has no configuration for the run's agent while an entry is unresolved |
| warning | `run ended: configuration unavailable` | A worker has no configuration for the run's workspace or agent |
| error | `job processing failed` | A handler raised; the lease expires and the job is delivered again |
| warning | `telemetry unavailable; continuing without traces and metrics` | Telemetry setup failed |

## Traces

A run is one trace, across processes and restarts.

1. The API's request span for `POST /v1/tasks/runs` is the run's root. Its context is stored on the run row (`runs.trace_parent`) in the same transaction that creates the run. The API never continues a trace supplied by the caller.
2. Every job row stores the context of the span that enqueued it (`jobs.trace_parent`).
3. A worker opens a span per delivery, named `job <kind>`, as a child of the run's root and linked to the span that enqueued the job. A job that is delivered again after a crash joins the same trace.
4. Approval decisions, reconciliations and cancellations open `approval.decide`, `ledger.reconcile` and `run.cancel` inside the run's trace, linked to their own request.
5. Audit events record the trace id in their `trace_id` column. It lies outside the MAC, so it is a correlation aid, not integrity-protected data.

| Span | Opened by | Notes |
|---|---|---|
| `<METHOD> <route template>` | API middleware | Method, route template and status only; never the query string, headers or body. No span for `/healthz` and `/readyz`. |
| `job <kind>` | Worker | Attributes include the job's id, kind, attempt, `max_attempts`, seconds waited, and `tether.job.outcome`. |
| `run.drive` | Run driver | One advance of the run graph. |
| `model.call` | Run graph | Provider, model, turn, attempt, outcome, number of tool calls. One span per provider attempt. |
| `policy.govern` | Run graph | A policy decision. |
| `tool.authorize` | Run graph | The pre-dispatch check and ledger authorization. |
| `tool.execute` | `execute_tool` handler | Tool name and version, effect, attempt, outcome. |
| `tool.verify` | `verify_outcome` handler | One verification probe. |
| `approval.decide` | Approvals route | Tool and new status. |
| `ledger.reconcile` | Ops route | Tool and reconciliation outcome. |
| `run.cancel` | Tasks and ops routes | |
| `replay.divergence` | Run graph | A persisted decision did not match its recorded hash; see [run lifecycle](../architecture/run-lifecycle.md). |

Events on spans: `approval.requested`, `run.finished` (with the status), `tool.retry_scheduled`, `tool.output_invalid` and `job.lease_lost`. Spans carry identifiers and bounded descriptors only, never payloads, arguments, outputs or keys. A failing span records the exception type, never its message. LangSmith and LangChain tracing are forced off in both processes, because they would ship prompts and tool payloads to a hosted service.

To find the trace of a run, take any log line for the run and read its `trace_id`, or query an audit event's `trace_id`.

## Metrics

Metrics are exported over OTLP/HTTP every `TETHER_OTEL_METRIC_INTERVAL_SECONDS` (default 15 s). Labels come from small fixed sets; they never include a run, user or workspace id or an argument. State-change metrics are recorded only after the transaction commits, so a rolled-back transaction counts nothing. Durations are histograms in seconds.

| Metric | Type | Labels | What it tells you |
|---|---|---|---|
| `tether.runs.started` | counter | none | Runs submitted |
| `tether.runs.finished` | counter | `status` (a terminal run status) | How runs end |
| `tether.run.duration` | histogram | `status` | Submission to terminal status. Not recorded for runs ended by a dead letter or by missing configuration |
| `tether.job.pickup_latency` | histogram | `kind` | How long a due job waited before a worker leased it. Rises when workers are saturated or absent |
| `tether.job.duration` | histogram | `kind`, `outcome` | Processing time of one delivery. `outcome` is `completed`, `retry`, `contended`, `stale`, `fenced`, `dead_letter`, `lease_lost` or `error` |
| `tether.jobs.queued` | gauge | `kind`, `state` | Jobs by kind and state: due, scheduled (future `run_after`) or leased. Sampled by workers |
| `tether.jobs.dead_lettered` | counter | `kind` | Jobs that exhausted their deliveries |
| `tether.jobs.lease_lost` | counter | `kind` | Deliveries aborted because the lease was lost |
| `tether.tool.duration` | histogram | `tool`, `outcome` | One tool send. `outcome` is `completed`, `not_applied`, `output_invalid` or `unknown` |
| `tether.tool.retries` | counter | `tool` | Sends scheduled again after an unknown outcome |
| `tether.ledger.indeterminate` | counter | `tool` | Writes whose outcome became `indeterminate` |
| `tether.verifications` | counter | `tool`, `result` | Verification probes and their ends. `result` is `confirmed_applied`, `confirmed_not_applied`, `inconclusive`, `exhausted` or `late` |
| `tether.reconciliations` | counter | `outcome` (`applied`, `not_applied`, `abandon`) | Entries a person resolved |
| `tether.policy.decisions` | counter | `decision`, `tool` | Recorded policy decisions. An unregistered tool is labelled `unregistered` |
| `tether.approval.wait_duration` | histogram | `status` (`approved`, `rejected`, `expired`) | Time from approval request to its resolution |
| `tether.model.call.duration` | histogram | `provider`, `outcome` (`succeeded`, `failed`) | One provider attempt |
| `tether.access_events.dropped` | counter | none | Access events (who inspected which run) that were not recorded |

The API also exports FastAPI's own HTTP server metrics (method, route template and status), which are not listed above.

The queue-depth gauge is a queue-wide snapshot, and every worker reports the same one. Combine workers with `max`, not `sum`. The gauge reports only the (`kind`, `state`) groups that currently have rows, so an idle system reports nothing: treat a missing series as 0. Absence means either an empty queue or no worker sampling it, so it cannot tell you a worker is down. Alert on worker health (`tether-worker-healthcheck`) for that.

### What to alert on

Tether publishes no thresholds, because none have been measured for your workload. Choose them from your own baseline. These are the signals worth an alert, and why.

| Signal | Why it matters |
|---|---|
| `tether.jobs.dead_lettered` above zero | A job used all its deliveries. This is never normal. What it means depends on the kind: an `advance_run` job logs `run dead-lettered`, writes a `DEAD_LETTERED` audit event and ends the run `failed` with `system_error` (or holds it in `awaiting_reconciliation` if a ledger entry is unresolved); an `execute_tool` job logs `job dead-lettered` and records the ledger outcome (`TOOL_OUTCOME`) with no `DEAD_LETTERED` event; `verify_outcome` and the timers log nothing and appear only in this counter. Follow [a dead job](runbooks.md#a-dead-job). |
| `tether.ledger.indeterminate` above zero | A write's outcome is unknown. Automatic verification may resolve it, but you want to know that it happened. |
| `tether.verifications` with `result=exhausted` above zero | Verification gave up. A person must establish the truth and reconcile, and the run is waiting. |
| Runs in `awaiting_reconciliation` (no metric reports this; query `tether.runs` for the status) | A run is parked until a person acts. |
| `tether.jobs.queued` with `state=due` growing, or high `tether.job.pickup_latency` | Workers are saturated, stopped, or cannot reach the database. |
| Worker container health failing | The worker's heartbeat is stale: the database is unreachable, or one job has outlasted the heartbeat max age. A missing `tether.jobs.queued` series is not a reliable signal. |
| `tether.jobs.lease_lost` above zero | A worker lost a lease: database trouble, or a worker too starved to heartbeat. The ledger rules keep this safe, but it signals instability. |
| `tether.runs.finished` with `status` `failed` or `timed_out` rising | Provider trouble, budgets exhausted, or deadlines too tight. |
| `tether.model.call.duration` with `outcome=failed` rising | Provider outage, credential problem or rate limiting. |
| `tether.approval.wait_duration` high, or `status=expired` rising | Approvers are not seeing requests in time. |
| `tether.access_events.dropped` above zero | The record of who inspected runs is incomplete. This does not affect the audit chain. |

Also use the container health checks: `/readyz` for the API, `tether-worker-healthcheck` for each worker ([deployment](deployment.md#first-time-setup)).

## The development stack

`deploy/compose/docker-compose.yml` runs an OpenTelemetry collector and Jaeger for the development stack only. The collector (`deploy/compose/otel-collector.yaml`) receives OTLP on ports 4317 (gRPC) and 4318 (HTTP) from `api` and `worker`, batches, sends traces to Jaeger, and prints metrics to its own log with the `debug` exporter:

```
docker compose -f deploy/compose/docker-compose.yml logs otel-collector
```

The Jaeger UI is published on port 16686. The stack includes no Prometheus and no Grafana, and Tether ships no dashboards or alert rules. For production, point `TETHER_OTEL_ENDPOINT` at your own collector and route its metrics pipeline to your monitoring system.

## Related

- [Runbooks](runbooks.md)
- [Audit](../governance/audit.md): the authoritative record
- [Secrets and data handling](../security/secrets-and-data-handling.md)
- [Configuration](configuration.md#telemetry)
