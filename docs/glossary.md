# Glossary

The terms the Tether documentation uses, in alphabetical order. Each entry gives a short
definition and links to the page that explains the concept. The pages use these words exactly.

**Access event.** A record that someone inspected a run through the approvals or ops audience,
written asynchronously and best effort to `tether.access_events`. It is not part of the audit
chain. See [Audit](governance/audit.md#access-events).

**Advisory lock.** A session-level PostgreSQL lock named after a run, which a worker must hold
to drive that run. Together with the fencing token it keeps one run driver per run. See
[Execution and recovery](architecture/execution-and-recovery.md#run-driver-lock-and-fencing-token).

**Agent.** A configured identity in a workspace that runs one workload's agent definition: its
system prompt and the tools it offers. An API key names which agents it may submit runs for.
See [Configuration](operations/configuration.md#the-bootstrap-file).

**API key.** A workspace credential (`tak_<key_id>_<secret>`) that identifies a calling
application on the tasks routes, together with an end-user token. Only a salt and a hash are
stored. See [Authentication and authorization](security/authentication-and-authorization.md#api-keys).

**Approval.** A person's permission for one exact tool call, bound to it by its call hash. It is
`pending` until it is `approved`, `rejected`, `expired` or `closed`. See
[Approvals](governance/approvals.md).

**Approver.** A person whose token carries the core `approver` role and a role named by the
tool's policy rule, and who is not the run's requester. See [Approvals](governance/approvals.md#who-may-decide).

**Audience.** The intended recipient named in a token's `aud` claim: `tether-tasks`,
`tether-approvals` or `tether-ops`. Each route accepts exactly one audience, taken from the
route, never from the token. See [Authentication and authorization](security/authentication-and-authorization.md#route-audiences).

**Audit chain.** A run's ordered audit events, each carrying an HMAC that covers the previous
event's MAC, so modification or reordering within the run is detectable by a holder of the
audit key. See [Audit](governance/audit.md).

**Audit event.** One record in a run's audit chain, such as `POLICY_DECISION` or `DISPATCHED`,
written in the same transaction as the state change it describes. See
[Audit](governance/audit.md#what-is-audited).

**Audit head.** The row in `tether.audit_heads` holding a run's last sequence number, last MAC
and pinned audit key. `tether audit verify` compares it with the events. See
[Audit](governance/audit.md#the-per-run-hmac-chain).

**Auditor.** A person with the `auditor` role on the ops audience, who may read any run of the
workspace. See [Authentication and authorization](security/authentication-and-authorization.md#roles).

**Automatic verification.** Running a write's declared probe, after a committed gate and a
policy `allow`, to resolve an `indeterminate` entry without a person. Implemented and tested;
not yet a stated guarantee. See [Unknown outcomes](governance/unknown-outcomes.md#automatic-verification).

**Binding key.** A secret HMAC key from the `TETHER_BINDING_KEY_<ID>` family, used only to
compute call hashes. It is a separate family from the audit keys. See
[Secrets and data handling](security/secrets-and-data-handling.md#the-two-key-families).

**Bootstrap file.** The reviewed YAML file that declares workspaces, agents, token issuers,
API-key hashes and policy, applied with `tether bootstrap apply`. It holds no secrets. See
[Configuration](operations/configuration.md#the-bootstrap-file).

**Call hash.** An HMAC-SHA-256, under a binding key, of the RFC 8785 canonical envelope of one
tool call: workspace, run, agent, tool, pinned version, validated arguments and preconditions.
See [Approvals](governance/approvals.md#what-an-approval-binds).

**Checkpoint.** LangGraph's saved graph state for a run, in the `langgraph` schema. It is a
progress cache that says where the graph is, never the record of what happened. See
[Data model](architecture/data-model.md#checkpoints).

**Dead-letter.** The transition that ends a job delivered past its maximum attempts: the job
becomes `dead` in the same transaction as a wake job and, for `execute_tool`, `advance_run` and
`verify_outcome`, a domain outcome for its work; timer jobs are only marked dead with a wake. See
[Execution and recovery](architecture/execution-and-recovery.md#dead-letter).

**Development identity shortcut.** In `dev` and `test` only, a request with no `Authorization`
header may state its identity in the body or query. In `prod` such a request is `401`. See
[Authentication and authorization](security/authentication-and-authorization.md#the-development-identity-shortcut).

**Development stack.** The Docker Compose stack in `deploy/compose/`, run with `TETHER_ENV=dev`
and the scripted model provider. It is not a production deployment. See
[Getting started](development/getting-started.md).

**Dispatch.** The committed transition of a ledger entry from `pending` to `dispatched`, written
with a `DISPATCHED` audit event before the tool is called. A permitted re-send records
`REDISPATCHED`. See
[Side effects and the execution ledger](governance/side-effects-and-ledger.md#three-commits-around-every-send).

**Effect.** Whether a tool is a `read` or a `write`, declared on its descriptor. A read may be
repeated; a write is sent under the ledger's re-send rules. See
[Writing a tool](development/writing-a-tool.md#the-tool-descriptor).

**Execution ledger.** The table `tether.ledger_entries`: one ledger entry per authorized tool
call, recording what was sent and what came back. See
[Side effects and the execution ledger](governance/side-effects-and-ledger.md).

**Fencing token.** The run's `driver_generation` counter. A run driver increments it when it
claims the run, and every one of its writes checks it, so a stale driver cannot write. See
[Execution and recovery](architecture/execution-and-recovery.md#run-driver-lock-and-fencing-token).

**Heartbeat.** The periodic extension of a job's lease while its handler runs, so a long
healthy tool call is not redelivered. Not to be confused with the worker's health file. See
[Execution and recovery](architecture/execution-and-recovery.md#lease-heartbeats).

**Idempotency key.** A deterministic key (`tth_` plus 52 base32 characters) derived from the
run, the step ordinal and the call hash. Every send of one logical execution carries the same
key. See [Side effects and the execution ledger](governance/side-effects-and-ledger.md#idempotency-keys).

**Idempotent write.** A write whose tool declares `idempotency="supported"`: its target applies
one request per idempotency key. Only such a write may be re-sent. See
[Side effects and the execution ledger](governance/side-effects-and-ledger.md#what-is-re-sent-and-what-never-is).

**`indeterminate`.** The ledger state of a dispatched write whose outcome is unknown and which
may not be sent again. It is never reported as a failure. See
[Unknown outcomes](governance/unknown-outcomes.md).

**Issuer.** A platform-controlled token signer configured per workspace in the bootstrap file,
with its `iss` value, allowed audiences, pinned algorithm and public keys. See
[Authentication and authorization](security/authentication-and-authorization.md#how-a-token-is-validated).

**Job.** One row in the PostgreSQL job queue (`tether.jobs`), of kind `advance_run`,
`execute_tool`, `verify_outcome`, `run_deadline` or `expire_approval`. A job is a queue item,
not a run. See [Execution and recovery](architecture/execution-and-recovery.md#the-job-queue).

**Lease.** A worker's time-limited claim on a job, with a random lease token. An expired lease
lets another worker take the job. See [Execution and recovery](architecture/execution-and-recovery.md#leasing).

**Ledger entry.** One row of the execution ledger: the pinned tool, call hash, idempotency key,
state, send attempts, outcome class and any resolution of one authorized call. See
[Side effects and the execution ledger](governance/side-effects-and-ledger.md#entry-states).

**MAC version.** The format of an audit event's MAC. Version 1 does not cover the event's
timestamp; version 2, used for every new event, does. See
[Audit](governance/audit.md#the-per-run-hmac-chain).

**Masking.** Replacing every field a tool marks `Sensitive` with a fixed mask, at one choke
point, before tool data is stored, audited or shown. See
[Secrets and data handling](security/secrets-and-data-handling.md#sensitive-tool-data).

**Model provider.** The service that answers model calls, reached through a provider adapter.
Two exist: the scripted provider and OpenAI. See [Model providers](operations/model-providers.md).

**Model turn.** One successful model response. If it proposes several tool calls, they are
governed one at a time, in order. See [Run lifecycle](architecture/run-lifecycle.md#the-model-turn-loop).

**Outcome class.** The classification recorded with every outcome: `applied`, `not_applied`,
`unknown`, or `failed` for a read whose output violates its schema. See
[Side effects and the execution ledger](governance/side-effects-and-ledger.md#outcome-classes).

**Pinned version.** The tool version fixed when a call is proposed. Exactly that version is
decided, approved, authorized and executed, or nothing is. See
[Approvals](governance/approvals.md#tool-version-pinning).

**Policy decision.** The answer to a proposed tool call: `allow`, `deny` or `require_approval`,
computed only from trusted inputs and recorded as a `POLICY_DECISION` audit event. See
[Policy](governance/policy.md).

**Policy version.** The SHA-256 of a workspace's canonical policy section together with the
evaluator version. A process decides only when its loaded version is the active one. See
[Policy](governance/policy.md#policy-versions).

**Precondition.** A required input field of a write whose value the target checks atomically
with the write. It is part of the call hash and shown to the approver. See
[Approvals](governance/approvals.md#preconditions).

**Probe.** The read-only function a write tool declares as its read-back. It answers
`confirmed_applied`, `confirmed_not_applied` or `inconclusive`, with evidence. See
[Unknown outcomes](governance/unknown-outcomes.md#the-read-back).

**Proposal.** A tool call the model asks for. It is untrusted until it is validated against the
tool's input schema and decided by policy. See [Run lifecycle](architecture/run-lifecycle.md#the-run-graph).

**Provider adapter.** The one module that translates Tether's provider-neutral history into a
vendor's API and back. See [Adding a model provider](development/adding-a-provider.md).

**Re-check before execution.** The evaluation of current policy, and of the approval's status,
hash and expiry, just before a ledger entry is created. A refusal writes no audit event of its
own. See [Approvals](governance/approvals.md#the-re-check-before-execution).

**Read-back.** A write tool's declared way to find out, after an unknown outcome, whether the
write took effect, without repeating it. Its function is the probe. See
[Unknown outcomes](governance/unknown-outcomes.md#automatic-verification).

**Reconciler.** A person whose token carries the core `reconciler` role and a role in the
policy's `reconciler_roles`, and who is not the run's requester. See
[Unknown outcomes](governance/unknown-outcomes.md#who-may-reconcile).

**Reconciliation.** A person resolving an `indeterminate` ledger entry as `applied`,
`not_applied` or `abandon`, with a justification, recorded as a `RECONCILED` audit event. See
[Unknown outcomes](governance/unknown-outcomes.md#human-reconciliation).

**Reconciliation hold.** A run in `awaiting_reconciliation` stays held, with no model call,
while any of its ledger entries is `pending`, `dispatched` or `indeterminate`. See
[Run lifecycle](architecture/run-lifecycle.md#the-reconciliation-hold).

**Replay.** LangGraph re-running a graph node after a resume or a crash. Every write node finds
its earlier row by a deterministic key and checks its call hash. See
[Run lifecycle](architecture/run-lifecycle.md#replay-and-replay-divergence).

**Replay divergence.** A replayed call whose recomputed call hash differs from the persisted
row's. Tether records `REPLAY_DIVERGENCE`, sends nothing further and ends the run. See
[Run lifecycle](architecture/run-lifecycle.md#replay-and-replay-divergence).

**Requester.** The person whose end-user token submitted a run. Not a role: the requester can
read and cancel their own run but never approve or reconcile it. See
[Authentication and authorization](security/authentication-and-authorization.md#roles).

**Resolution.** How an `indeterminate` entry was settled: `verified_applied` or
`verified_not_applied` by a probe, `reconciled_applied`, `reconciled_not_applied` or
`reconciled_abandon` by a person. See [Unknown outcomes](governance/unknown-outcomes.md).

**Retained version.** An earlier tool version a workload keeps resolvable for calls already
pinned to it. It is never offered to a new model turn. See
[Writing a tool](development/writing-a-tool.md#versions).

**Run.** One agent task submitted by a requester, from `queued` to a terminal status. Not to be
confused with a job. See [Run lifecycle](architecture/run-lifecycle.md).

**Run driver.** The worker code that holds a run's advisory lock and fencing token and advances
its graph. It is the only writer of a run's status. See
[Run lifecycle](architecture/run-lifecycle.md#who-writes-the-status).

**Scripted provider.** A model provider that replays fixed scenarios instead of calling a model.
It loads only when `TETHER_ENV` is `dev` or `test`. See [Model providers](operations/model-providers.md).

**Send budget.** A tool's `max_attempts`: the sends one call may use, shared by retries and
redeliveries. A non-idempotent write is sent at most once whatever it is. See
[Side effects and the execution ledger](governance/side-effects-and-ledger.md#what-is-re-sent-and-what-never-is).

**`Sensitive`.** The `tether.sdk` marker a tool author puts on an input or output field so that
masking replaces its value. See [Secrets and data handling](security/secrets-and-data-handling.md#sensitive-tool-data).

**Skipped call.** A later call of a model turn that is not governed because an earlier call did
not apply. It gets a `CALL_SKIPPED` event and a typed `skipped` result. See
[Run lifecycle](architecture/run-lifecycle.md#the-model-turn-loop).

**Step ordinal.** The position number Tether gives each proposed tool call in a run. Approvals,
ledger entries and idempotency keys are keyed by it. See
[Run lifecycle](architecture/run-lifecycle.md#the-model-turn-loop).

**Stop.** A cancellation or a passed deadline. The stop predicate is checked before every model
call, decision, authorization, send and probe; a stop never recalls a dispatched write. See
[Run lifecycle](architecture/run-lifecycle.md#stops).

**Terminal status.** `succeeded`, `failed`, `cancelled`, `timed_out` or `abandoned`. A database
trigger forbids any change to it. See [Run lifecycle](architecture/run-lifecycle.md#statuses).

**Terminating result.** A result that ends the run's governance of the current turn with no
further model call, such as an exhausted repair or denial budget or an abandon. See
[Run lifecycle](architecture/run-lifecycle.md#terminating-results).

**Tool.** A capability a workload offers the agent, declared by a `ToolDescriptor` with its
schemas, effect, risk, permissions and send rules. See [Writing a tool](development/writing-a-tool.md).

**Tool call.** One invocation of a tool with arguments. What the model asks for is a proposal;
once authorized it has a ledger entry. See
[Side effects and the execution ledger](governance/side-effects-and-ledger.md).

**Wake job.** An `advance_run` job enqueued by every transaction that changes something a run
waits for. Wake jobs are never coalesced. See
[Execution and recovery](architecture/execution-and-recovery.md#the-job-queue).

**Workload.** A package of tools and one agent definition that imports only `tether.sdk`. It is
a client of Tether, never part of it. See [Writing a tool](development/writing-a-tool.md#workloads-and-the-import-rule).

**Workspace.** The isolation boundary for configuration and data. Every request-path query after
authentication is scoped to one workspace; anything outside it is `404`. See
[Authentication and authorization](security/authentication-and-authorization.md#workspace-isolation).

**Workspace admin.** A person with the `workspace_admin` role on the ops audience, who may read
and cancel any run of the workspace. See
[Authentication and authorization](security/authentication-and-authorization.md#roles).
