# Secrets and data handling

Where Tether's secrets come from, how its two key families are kept apart, how sensitive tool data is masked, and what logs, traces and metrics may contain. For security reviewers, operators and tool authors. Rotation procedures are in [key lifecycle](../operations/key-lifecycle.md); the wider threat analysis is in the [security model](security-model.md).

## Where secrets come from

Secrets reach Tether through the environment, through one interface, and nowhere else. The repository, the bootstrap file and the container image hold none.

| Secret | Read by | How it is loaded |
|---|---|---|
| `TETHER_DATABASE_URL` | `api`, `worker` | A `SecretStr` setting. Never rendered in a repr or dump. The URL and its password are registered with the log scrubber at startup. |
| `TETHER_MIGRATOR_DATABASE_URL` | `tether migrate`, `tether bootstrap apply` | Used only by the one-shot commands, never by `api` or `worker`. Not registered with the log scrubber. |
| `TETHER_AUDIT_KEY_<ID>`, `TETHER_BINDING_KEY_<ID>` | `api`, `worker` | Through the key ring, which reads the secrets provider. Registered with the log scrubber on first load. |
| `TETHER_OPENAI_API_KEY` | `worker` | Through the secrets provider only. It is deliberately not a setting, so it cannot reach a settings dump. The adapter registers it with the log scrubber. |
| Workload credentials | The workload's tool code | Whatever the workload does. The change-control reference workload reads `CHANGE_CONTROL_GITHUB_TOKEN` directly from the process environment and puts it only in an `Authorization` header. It is outside the secrets provider and is not registered with the scrubber; only the `Bearer` shape in a log line would be caught. |

Two settings decide where credentials are sent: `TETHER_OPENAI_BASE_URL` for the provider key, and `CHANGE_CONTROL_GITHUB_API_URL` for the GitHub token. Anyone who can set the process environment can redirect these credentials.

The secrets provider (`src/tether/secrets/`) is the seam for a future secret manager. The only implementation reads a snapshot of the process environment taken at construction, and keeps **only** variables that start with `TETHER_`, so unrelated secrets in the environment never sit in the object. Rotating a secret needs a process restart.

Secret values are wrapped in `SecretValue`, which renders as `**********` in `repr`, `str` and formatting and refuses to be pickled; copying it returns the same masked wrapper. The provider and the key ring also refuse pickling and print `<redacted>`, so neither can end up in a job payload, a checkpoint or a log line by accident. A secret that is not set raises an error and the caller fails closed.

Deployment rules that follow: keep real values out of any file in the repository, deliver them through the host's secret mechanism, and use the placeholders in `deploy/compose/.env.production.example` as the checklist of what to set. The development Compose stack reads a git-ignored env file and is not a production deployment.

## The two key families

| | Audit keys | Call-binding keys |
|---|---|---|
| Variables | `TETHER_AUDIT_KEY_<ID>`, `TETHER_AUDIT_ACTIVE_KEY_ID` | `TETHER_BINDING_KEY_<ID>`, `TETHER_BINDING_ACTIVE_KEY_ID` |
| Protects | The per-run audit chain ([audit](../governance/audit.md)) | The call hash that binds an approval to one exact call ([approvals](../governance/approvals.md)) |
| Pinned | On the run's audit head; each event records its key ID | On the run, before its first call hash is computed |
| Computed by | `api` and `worker`, when appending or verifying events | `worker`, when governing and dispatching calls |

Both are 32 random bytes in canonical base64url (unpadded or with its `=`). Any other spelling of the same bytes is rejected. A key ID is 1 to 32 characters of `A-Z` and `0-9`; uppercase only, because environment variable names are case-insensitive on Windows and lowercase IDs could silently alias. A missing, malformed or wrongly sized key raises an error and the operation fails closed: no decision, no dispatch, no audit event.

**Why they are separate.** The call hash is shown to approvers next to the masked arguments. If it were a plain SHA-256 of the unmasked arguments, anyone who sees it could hash guesses and recover a low-entropy `Sensitive` value, which defeats masking. The hash is therefore an HMAC under a key that never sits in the database and that no viewer holds. That key is distinct from the audit key so that each secret has one purpose and a leak of one does not forge the other. See [ADR-0010](../adr/0010-keyed-canonical-call-hash.md).

Tether does not check at startup that the two families hold different material. Generate each key independently and keep them in separate entries.

## What the bootstrap file may contain

The bootstrap file is reviewed, version-controlled configuration. It may contain:

- workspace and agent definitions and the policy;
- issuer **public** keys in PEM form;
- API key IDs, salts and SHA-256 hashes.

It may not contain private keys, raw API keys, audit or binding keys, the provider API key or database credentials. The loader rejects a PEM private key and a public key of the wrong type for the issuer's algorithm. It cannot recognise a secret placed in some other field, so review the file before applying it. See [authentication and authorization](authentication-and-authorization.md#provisioning-identities).

## Sensitive tool data

A tool author marks a field `Sensitive` on the tool's input or output model:

```python
from typing import Annotated

from pydantic import BaseModel

from tether.sdk import Sensitive


class RotateCredentialInput(BaseModel):
    account: str
    new_password: Annotated[str, Sensitive]
```

Tool arguments and outputs stored in approval, ledger and audit rows, and shown in views, pass through one function, `mask` in `src/tether/registry/masking.py`. The exceptions are in the table below. It replaces each `Sensitive` field with `**********`, recursively through nested models, lists and dictionaries. A `Sensitive` field that holds a structure is replaced whole. There is one rule for every viewer; there are no field-level views ([ADR-0017](../adr/0017-sensitive-data-masking-choke-point.md)).

`Sensitive` must annotate the field itself, for example `Annotated[str | None, Sensitive]`. `Sensitive` on an element type (such as `list[Annotated[str, Sensitive]]`) or inside a union member is not detected, and that data is stored and shown unmasked.

The call hash is computed over the unmasked, validated arguments, so an approval binds the real value even though nobody sees it.

### What is stored, and what is masked

| Data | Where | Masked |
|---|---|---|
| Arguments in an approval request | `approvals.arguments_masked`, `approvals.preconditions_masked`, the `APPROVAL_REQUESTED` and `EXECUTION_AUTHORIZED` audit payloads | Yes |
| Tool output | `ledger_entries.outcome_payload_masked`, the `TOOL_OUTCOME` audit payload | Yes, before the outcome transaction |
| Verification evidence | `ledger_entries.verification_evidence_masked` | Yes |
| Validated arguments | The `execute_tool` job payload, which the worker needs to send the call | **No** |
| The model's history and proposals | The LangGraph checkpoint (the `langgraph` schema) | **No** for what the model proposed; tool results in it are the masked ones |
| Task input | `runs.task_input`, stored as received, and the first message the model receives | **No**: free text from the caller |
| A run's `outcome_summary` | `runs.outcome_summary`, returned in run views | **No**: the model's final text in a successful run; a Tether-authored reason, which can include a provider status code, in a failed one |
| An approval reason and a reconciliation justification | The `APPROVAL_DECIDED` and `RECONCILED` audit payloads | **No**: free text from a person |
| A tool's `ToolNotApplied` reason | `ledger_entries.outcome_reason`, and the result the model sees | **No**: text from the tool author |

Rows that hold unmasked data are listed above. Unmasked arguments exist in the job payload and the checkpoint, and Tether does not encrypt either at rest. Use storage-level encryption and restrict who can read the database. Nothing in a database reader's path is masked.

A field the tool author does not mark is not masked. Mark every field that carries a credential, personal data or anything else that should not appear in an approval screen, a ledger row or a log. The reference workloads declare no `Sensitive` fields. Free-text fields (task input, reasons, justifications) are not masked, so do not put secrets in them.

If masking cannot complete because a model is malformed, `mask` raises. A write is then an unknown outcome and a read a typed failure. It is never silent. A write whose output violates the tool's output schema is recorded as applied with a marker; the invalid output itself is never stored, logged or shown.

### What a person can see

| Viewer | Through | Sees |
|---|---|---|
| Requester | `GET /v1/tasks/runs/{run_id}` | Run status, approvals with masked arguments, ledger entries with masked outcomes and evidence, and a list of audit events (sequence, type, actor, tool, policy version) with `audit_chain_valid`, which recomputes the chain but does not check the persisted head. Use `tether audit verify` for that. |
| Approver | Approval list, detail and run evidence | The tool, version, masked arguments and preconditions, call hash, requester, agent, expiry and decision. Not the model's rationale, the tool's risk or the task input. |
| Auditor, workspace admin | `GET /v1/ops/runs/{run_id}` | The same view as the requester, for any run in the workspace. |

None of these views returns an audit event's payload, checkpoint data or task input. They read Tether's own tables, never traces.

An approver judges a masked view. If a value that the approver must verify is marked `Sensitive`, the approver cannot verify it. Mark only what should stay hidden.

### What reaches the model

The model receives the agent's system prompt, the task message, its own earlier messages, the tool manifest and tool results. Tool results are the masked outcomes, so the model never sees a `Sensitive` output and cannot reason over one. A value that originated in the task input or in the model's own proposal is already known to it.

With a hosted provider, everything the model receives leaves the deployment. That includes non-sensitive tool output. A hosted provider's data handling is outside Tether's control; see [model providers](../operations/model-providers.md).

## Logs, traces and metrics

Telemetry is best effort. A failure in logging, tracing or export never blocks a request, a job or shutdown.

**Logs** are JSON, correlated by `run_id`, `workspace_id`, `job_id`, `tool`, `trace_id` and `span_id`. Library records (the web server, the exporter, LangGraph) go through the same pipeline. Every record passes through redaction (`src/tether/observability/logging.py`):

- top-level log fields named like payloads or credentials are dropped, including `payload`, `arguments`, `task_input`, `history`, `state`, `output`, `messages`, `body`, `headers`, `authorization`, `api_key`, `x-api-key`, `token`, `password` and `secret`;
- registered secret values (the database URL and password, every audit and binding key that has been loaded, the provider API key) are replaced wherever they appear, including in exception text, if they are at least 8 characters;
- bearer tokens, JWTs and Tether API keys are replaced by their shape;
- tracebacks are rendered as text without local variables;
- request-line logging by the HTTP client libraries is silenced, because a URL can carry tool arguments.

Nested fields and free-text messages are only pattern-scrubbed: dropping applies to top-level field names. The scrubber knows only registered values and these shapes. A secret that is neither, such as the migrator database URL or a workload credential outside the secrets provider, is not removed from a message that contains it. Do not log secrets from tool code.

Graph state, job payloads, task input, arguments and outputs are never logged. Validation errors report field locations and error types, never the input values. Third-party agent tracing that would ship prompts and payloads to a hosted service is forced off.

**Traces** carry identifiers and bounded descriptors, never payloads, arguments, outputs, tokens or keys. A failing span records the exception's type, not its message. The request span records the HTTP method, the route template and the status code, with no headers, query string or body, and never continues a trace id sent by a caller. Spans leave the process only when `TETHER_OTEL_ENDPOINT` is set, over OTLP/HTTP to a collector you operate.

**Metrics** use bounded labels only: status, outcome, job kind, provider, decision and the registered tool name (a call to an unregistered tool is labelled `unregistered`). No label carries a run, a requester, an argument or a value from a request. See [observability](../operations/observability.md) for the instruments.

## What the production image leaves out

`docker build --target prod .` builds an image that holds the core and the real workload. It deliberately excludes the following; the build and a test of the built image check this.

- The demonstration workload, every scenario directory and the scripted model provider.
- The `config/` directory and any bootstrap file. The operator mounts a reviewed file.
- Any `.env` file and the development token issuer (`tools/dev_issuer/`, which is in neither image variant).
- Development tooling.

The image runs as a non-root user, chooses neither its environment nor its bootstrap file, and in `prod` serves no OpenAPI schema. It does still contain the development identity module, which is inert outside `dev` and `test` ([authentication and authorization](authentication-and-authorization.md#the-development-identity-shortcut)). Production configuration steps are in [production configuration](../operations/production-configuration.md).

## Limits

- Masking depends on the tool author's annotations.
- Unmasked arguments exist in the checkpoint and the job payload, and there is no encryption at rest in Tether.
- Free-text fields are not masked.
- The scrubber removes only registered values and known shapes.
- No data-retention policy exists. The application database role has no `DELETE` on the `tether` schema tables, so nothing there is removed. It can delete from and update the `langgraph` checkpoint schema, which holds unmasked model proposals.
- `Sensitive` on an element type or inside a union member is not detected.
- Audit and binding key independence is not checked at startup.

## Related

- [Security model](security-model.md)
- [Authentication and authorization](authentication-and-authorization.md)
- [Key lifecycle](../operations/key-lifecycle.md)
- [Observability](../operations/observability.md)
- [Audit](../governance/audit.md)
