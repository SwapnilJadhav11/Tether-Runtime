# Tether V1 Guarantee Card

> **Status:** draft for owner acceptance (direction record TDR-015, U-004)
> **Evidence baseline:** commit `ec352540a8fd9f70309b12af53f013b87c2258e4` (`hardening/v1-baseline`, CI green 2026-11-09)
> **Rule:** §1 contains only statements the baseline implementation proves with automated tests. Anything else is in §2 (planned) or §3 (limits). A guarantee moves from §2 to §1 only with the code, the tests and an owner review.

Each guarantee states the claim, its boundary conditions, where it is implemented, which tests prove it, what happens on failure or ambiguity, and known limitations. Test names are the functions in `tests/`; `poe` tasks are defined in `pyproject.toml`.

## 1. Guarantees proven at the baseline

### G-1 No external side effect without committed authorization and dispatch records

- **Statement.** A tool is never executed unless a ledger entry for that exact call was created by the run driver under current policy (TX5) and the entry was moved to `dispatched`, with its audit event, in a transaction that committed before the external call (TX6).
- **Boundary.** Applies to tools executed through Tether's worker. Graph nodes never perform side effects.
- **Implementation.** `src/tether/runtime/graph.py` (`_authorize`, TX5); `src/tether/worker/handlers/execute_tool.py` (TX6, payload-hash check before dispatch).
- **Evidence.** `tests/integration/test_run_graph.py` (payload-hash mismatch → `pending → failed`, no call), `tests/integration/test_slice_run_lifecycle.py`, `tests/chaos/test_chaos_crashes.py::test_c01_c04_a_crash_after_each_commit_resumes_without_duplicates`, `tests/chaos/test_chaos_races.py::test_s5_1_cancel_vs_authorization_race_repeated_50_times`.
- **Failure behaviour.** A failed audit append or a stop observed in TX5/TX6 aborts the transaction; the call is `not_applied` and nothing left Tether.
- **Limitations.** Create-or-get on replay does not yet compare the stored call hash (planned P-1); the fresh path does.

### G-2 A non-idempotent write is sent at most once

- **Statement.** Once a non-idempotent write has been dispatched, Tether never automatically re-sends it. Any unknown outcome becomes `indeterminate`.
- **Boundary.** "Non-idempotent" is the tool author's declaration (`IdempotencySupport` on the descriptor; writes must declare it).
- **Implementation.** `src/tether/worker/handlers/execute_tool.py` (unknown-outcome branch), `src/tether/sdk/tools.py` (declaration required for writes).
- **Evidence.** `tests/chaos/test_chaos_races.py::test_c07_f7b_lease_expiry_on_a_non_idempotent_write_is_indeterminate_never_resent`, `tests/chaos/test_chaos_crashes.py::test_c07_a_crash_with_a_non_idempotent_write_in_flight_is_held_then_reconciled`, `tests/integration/test_retries_dead_letter.py`.
- **Failure behaviour.** Worker death, lease expiry or a dropped connection after dispatch → `indeterminate` → the run waits for reconciliation.
- **Limitations.** None known.

### G-3 Idempotent writes are re-sent only with the same key, within a bound, and never after a stop

- **Statement.** Retries and redeliveries of one logical execution present the same deterministic idempotency key, share one attempt counter bounded by the descriptor, and each re-send is preceded by a committed gate that observed the stop predicate false.
- **Implementation.** `src/tether/core/hashing.py` (key derivation), `graph.py` (key stored at TX5), `execute_tool.py` (TX6r gate), `src/tether/queue/backoff.py`.
- **Evidence.** `tests/unit/test_hashing.py`, `tests/unit/test_tool_retry_budget.py`, `tests/integration/test_retries_dead_letter.py`, `tests/chaos/test_chaos_races.py::test_c07_overlapping_same_key_sends_apply_once`, `tests/chaos/test_chaos_crashes.py::test_c05_c06_a_crash_with_an_idempotent_write_in_flight_applies_it_once`.
- **Failure behaviour.** Budget exhausted with an unknown outcome → `indeterminate`; stop observed before a re-send → no send, `indeterminate` in the same transaction.
- **Limitations.** The target must retain idempotency keys for longer than recovery takes; the demo target does.

### G-4 Unknown outcomes are never reported as failure; at the baseline only a human resolves them

- **Statement.** A side-effecting result is `not_applied` only with positive evidence; everything else after dispatch is `unknown` and becomes `indeterminate`. The model is never told such a write failed. The entry is resolved only by an eligible reconciler (role, policy eligibility, not the requester) with a justification, recorded as a critical audit event before the run resumes or terminates.
- **Implementation.** `execute_tool.py` (classification), `src/tether/api/routers/ops.py` (TX8), `src/tether/policy/engine.py` (eligibility).
- **Evidence.** `tests/integration/test_reconciliation.py` (13 cases), `tests/unit/test_reconciliation_eligibility.py`, `tests/chaos/test_chaos_crashes.py::test_a_send_budget_spent_by_crashes_is_indeterminate_then_reconciled`.
- **Failure behaviour.** No eligible reconciler → the run waits without holding compute.
- **Limitations.** This is the baseline behaviour (former ADR-0007 rules 7–8). ADR-0019 (accepted 2026-11-09) requires tool-declared automatic verification to be tried first; that is planned P-4, not current behaviour. The "never reported as failure" part is unchanged by ADR-0019.

### G-5 After a stop, no model call starts and no proposal is governed

- **Statement.** Once cancellation is requested or the stored deadline has passed, Tether starts no further model call and governs no further proposal. A stopped run whose write is still unresolved waits, and reconciliation then only records the outcome and terminates the run.
- **Implementation.** `graph.py` (stop checks at the gate, after every model response, in TX2/TX3/TX5), `execute_tool.py` (TX6/TX6r), `src/tether/worker/handlers/timers.py` (TX10), `src/tether/api/routers/tasks.py` and `ops.py` (TX9).
- **Evidence.** `tests/integration/test_cancellation_deadlines.py` (14 cases), `tests/chaos/test_chaos_races.py::test_s5_1_approve_vs_cancel_race_repeated_50_times`, `::test_s5_1_cancel_vs_authorization_race_repeated_50_times`, `::test_a_stop_while_a_write_is_in_flight_never_recalls_it_and_takes_no_new_action`.
- **Failure behaviour.** A model request that started before the stop committed is not a "further" call; its proposals are recorded and discarded.
- **Limitations.** Status becomes `cancelled`/`timed_out` only when the driver next runs; clients read the stop flags alongside status.

### G-6 Dispatched writes are never recalled, and unknown outcomes are never hidden by a clean terminal status

- **Statement.** Cancellation or deadline never interrupts or recalls a dispatched write. A run is never shown as cleanly `cancelled` or `timed_out` while an outcome is unknown; the terminal summary names any reconciled outcome.
- **Implementation.** `graph.py` (await nodes never short-circuited by the stop flag; `finalize` summaries), ADR-0006 precedence.
- **Evidence.** `tests/chaos/test_chaos_races.py::test_a_stop_while_a_write_is_in_flight_never_recalls_it_and_takes_no_new_action`, `tests/integration/test_cancellation_deadlines.py`.
- **Limitations.** None known.

### G-7 Terminal run statuses never change

- **Statement.** Once a run is `succeeded`, `failed`, `cancelled`, `timed_out` or `abandoned`, its status cannot change, enforced by a database trigger independent of application code.
- **Implementation.** migration `rev_0002_core_schema.py` (`runs_status_terminal_guard`).
- **Evidence.** `tests/integration/test_schema.py`, `tests/chaos/harness.py::assert_terminal_stable` (asserted by the chaos suite).
- **Limitations.** Other columns remain writable by design (stop requests on a terminal run are no-ops).

### G-8 At most one run driver changes a run's authoritative state at a time

- **Statement.** Run driving is serialised by a session-level advisory lock and a per-run fencing token; a driver that lost its lock cannot write, and a contended job is requeued without consuming an attempt.
- **Implementation.** `src/tether/runtime/driver.py`, `src/tether/storage/repos/runs.py` (`guard`).
- **Evidence.** `tests/chaos/test_chaos_races.py::test_three_workers_racing_for_one_run_repeated_50_times`, `::test_a_driver_that_lost_its_lock_is_fenced_and_writes_nothing`, `::test_c07_four_workers_draining_fifty_runs_never_duplicate_work`, `tests/integration/test_run_graph.py` (stale generation).
- **Limitations.** The residual LangGraph checkpoint-fork window (ADR-0005 rule 6) is closed only once hash-checked create-or-get is implemented (planned P-1).

### G-9 A crash at any point loses no run and duplicates no supported write

- **Statement.** Killing the API or a worker at any instruction, including immediately after any critical transaction commits, leaves a run that resumes to the same outcome with no duplicate ledger rows and a target-side write count of one for supported writes.
- **Implementation.** ADR-0003 write/wait node discipline, `durability="sync"`, TX catalogue.
- **Evidence.** `tests/chaos/test_chaos_crashes.py::test_c01_c04_a_crash_after_each_commit_resumes_without_duplicates`, `::test_c02_a_crash_during_a_model_call_re_calls_the_turn`, `::test_c05_c06_a_crash_with_an_idempotent_write_in_flight_applies_it_once`, `tests/chaos/test_chaos_processes.py::test_c01_c03_hard_killing_the_api_and_worker_mid_run_loses_and_duplicates_nothing`, `tests/contract/langgraph/*`.
- **Failure behaviour.** A model call interrupted before its checkpoint is repeated; both attempts are recorded.
- **Limitations.** In-process crash points are a test harness; production processes have no fault points.

### G-10 Job exhaustion always produces a domain outcome

- **Statement.** A job leased past its maximum attempts is marked dead in the same transaction as a kind-specific transition: `execute_tool` → `failed` (not applied) before dispatch, `indeterminate` for a dispatched write, typed failure for a read; `advance_run` → `awaiting_reconciliation` if an entry is unresolved, else `failed` with `system_error`; always with a wake.
- **Implementation.** `src/tether/queue/postgres.py`, `execute_tool.py`, `src/tether/worker/handlers/advance_run.py`.
- **Evidence.** `tests/integration/test_retries_dead_letter.py`, `tests/chaos/test_chaos_crashes.py::test_tx12_advance_run_crashing_on_every_delivery_is_dead_lettered`, `::test_tx12_execute_tool_crashing_before_dispatch_is_definitely_not_applied`.
- **Limitations.** None known.

### G-11 Invalid credentials fail before any state change; out-of-scope resources do not exist

- **Statement.** Every credential failure (signature, issuer, audience, expiry, subject, workspace claim, API key, agent binding) returns `401` before any audit, policy or job activity. The expected audience comes from the route, never the token. Resources outside the caller's workspace or authorization return `404`, never `403`.
- **Implementation.** `src/tether/identity/`, `src/tether/authz/deps.py`, workspace-scoped repositories in `src/tether/storage/`.
- **Evidence.** `tests/integration/test_authentication.py` (12 cases), `tests/unit/test_identity.py`, `tests/integration/test_permissions.py`, `tests/unit/test_scope.py`, `tests/integration/test_uow.py`.
- **Limitations.** Proven for the routes that exist at the baseline; the full T-24 matrix with a second fixture workspace is planned (P-10). A dev/test identity shortcut exists when `TETHER_ENV ∈ {dev,test}` and no `Authorization` header is sent; see §3.

### G-12 An approval authorizes exactly the call it was given, and current policy still applies

- **Statement.** An approval is bound to an HMAC-SHA-256 call hash over the canonical (RFC 8785) envelope of the validated arguments. A call that differs from its decided hash is not authorized. Before execution, current policy is re-evaluated: a deny overrides an approval; a `require_approval` proceeds only with a matching approved, unexpired approval. The requester cannot approve their own request.
- **Implementation.** `src/tether/core/hashing.py`, `graph.py` (`_authorize`), `src/tether/api/routers/approvals.py`, `src/tether/policy/engine.py`.
- **Evidence.** `tests/unit/test_hashing.py` (29 cases), `tests/unit/test_canonical.py`, `tests/integration/test_run_graph.py` ("call changed since its decision"), `tests/integration/test_permissions.py`, `tests/chaos/test_chaos_races.py::test_approve_vs_expiry_race_repeated_30_times`.
- **Limitations.** Preconditions are not yet part of the envelope in practice (always empty); approval expiry is one global setting (per-tool expiry planned, P-8).

### G-13 Critical audit events are written inside the state-changing transaction and chained per run

- **Statement.** Every policy decision, approval request and decision, ledger transition, dispatch, reconciliation, stop request and status change is appended in the same transaction as the state it describes; if the append fails, the state change does not happen. Events form a per-run HMAC-SHA-256 chain keyed outside the database; in-place modification or reordering of events is detected.
- **Implementation.** `src/tether/audit/chain.py`, `src/tether/storage/repos/audit.py`, every TX function.
- **Evidence.** `tests/integration/test_audit_chain.py` (chain verifies, tamper detected, rollback with transaction, concurrent appends), `tests/integration/test_schema.py` (append-only grants), `tests/chaos/harness.py::assert_reconstructable` (run after chaos tests).
- **Limitations.** The MAC does not cover `created_at` (planned P-3; timestamps can be altered undetected). Tail truncation and whole-run deletion are not detected (design limit). Chain validity is exposed in run inspection; the `tether audit verify` command is planned (P-9). Skipped calls are not yet audited (planned P-2).

### G-14 Sensitive tool output is masked before it is persisted, logged, traced or shown

- **Statement.** Fields annotated `Sensitive` are masked by the worker before the outcome transaction; registered secret values and credential shapes are scrubbed from logs; graph state and job payloads are never logged.
- **Implementation.** `src/tether/registry/masking.py`, `src/tether/observability/logging.py`, `src/tether/secrets/`.
- **Evidence.** `tests/unit/test_masking.py`, `tests/unit/test_secrets.py`, `tests/unit/test_observability.py`, `tests/integration/test_observability.py`.
- **Limitations.** A field the tool author fails to annotate is not masked. Unmasked arguments exist in checkpoints and job payloads (encryption at rest is post-V1).

### G-15 Decisions are made only under the workspace's active policy version

- **Statement.** A policy decision or eligibility answer is recorded only if the evaluating process's loaded policy version equals the workspace's active version read in the same transaction; otherwise no decision is recorded, the job is requeued without consuming an attempt (worker) or the API returns `503` with no state change.
- **Implementation.** `graph.py` (`_assert_current_policy`), `src/tether/policy/`, bootstrap apply.
- **Evidence.** `tests/integration/test_run_graph.py::test_no_decision_is_made_under_a_stale_policy_version`, `tests/integration/test_bootstrap_apply.py`, `tests/unit/test_policy_config.py`.
- **Limitations.** Policy inputs are tool name and requester roles only; agent identity, permissions and risk are planned (P-6).

### G-16 Every request-path query is workspace-scoped

- **Statement.** Request handlers cannot obtain a repository without a workspace scope; cross-workspace lookups return not-found; composite foreign keys enforce workspace consistency in the schema.
- **Implementation.** `src/tether/storage/scope.py`, `repos/base.py`, migration `rev_0002`.
- **Evidence.** `tests/unit/test_scope.py`, `tests/integration/test_uow.py`, `tests/integration/test_schema.py`.
- **Limitations.** Tests use one real workspace; the second fixture workspace and T-24 are planned (P-10).

## 2. Planned guarantees (required for V1, not yet proven)

These are requirements, not current behaviour. Each becomes a §1 entry when implemented and tested.

| ID | Planned statement | Depends on | Target evidence |
|---|---|---|---|
| P-1 | Every create-or-get on replay compares the stored call hash; a mismatch is a `REPLAY_DIVERGENCE` that fails closed | D-6.1 | negative divergence test; plan step W2-K1 |
| P-2 | Every skipped call in a multi-call turn has a fenced decision-class audit event | D-6.2 | T-12 reconstruction; W2-K2 |
| P-3 | The audit MAC covers `created_at` through a versioned record; v1 chains still verify | D-6.3 | T-13 timestamp case; W2-K3 |
| P-4 | An unknown write outcome on a tool with a safe read-back is verified automatically, bounded, stop-gated and authorized, before human reconciliation; verification never re-sends | ADR-0019 (accepted); W5.1 | T-27 |
| P-5 | A healthy tool call longer than the lease is neither redelivered nor marked `indeterminate` while its heartbeat continues | TDR-010 | T-29 |
| P-6 | Policy decisions take requester, agent, workspace, permissions and risk as trusted inputs through a stable interface | TDR-008, U-008 | T-06 extended |
| P-7 | Malformed, unknown or repeatedly denied actions stop the run at a small explicit bound | D-11 | T-22 |
| P-8 | Approval expiry is per tool with a safe default and an explicit maximum | D-8 | T-15 |
| P-9 | `tether audit verify` recomputes any run's chain and exits non-zero on tampering | D-10 | T-13 |
| P-10 | Every endpoint × audience × role × workspace combination behaves per the authorization table (T-24), including approval list/detail and auditor/admin run views | D-10 | T-24, T-28 |
| P-11 | The same run executes unchanged on the scripted provider and on OpenAI with a configuration change only | TDR-003/004 | T-11 |
| P-12 | A governed write against the GitHub sandbox exercises approval, idempotency strategy, outcome classification, verification and audit with the same semantics as the demo target | U-001 | T-30 |
| P-13 | A second unrelated workload runs on the same kernel with no core change | U-002 | T-31, T-14 |
| P-14 | A production process fails startup without an explicit valid `TETHER_ENV`; dev-only components are unavailable in production | D-12 | T-32 |
| P-15 | Published operating limits (concurrency, latency, tool duration, lease) are measured and reproducible | U-006 | P-01–P-03 |

## 3. Operating limits and known limitations at the baseline

- **Not measured.** No performance or load suite exists; P-01–P-03 are targets. Development defaults: worker lease 60 s, poll 1 s, run deadline 3600 s, approval TTL 3600 s, model turn bound 8, tool send attempts 3, job attempts 8 (`advance_run`) / 5 (`execute_tool`).
- **No lease heartbeat.** A tool call longer than the lease is redelivered (idempotent) or becomes `indeterminate` (non-idempotent) even if healthy.
- **No production model provider.** The scripted provider is the driver's default and refuses only `prod`; no OpenAI adapter exists yet, and provider selection is not configurable.
- **Environment default.** `TETHER_ENV` defaults to `dev` when unset, which enables the dev identity shortcut and the scripted provider. D-12 requires this to fail closed.
- **Approvals.** Decision endpoint only; no list, detail or approver run view.
- **Audit.** Timestamps outside the MAC; no verify CLI; no audit query; access events not written; skipped calls not audited.
- **Replay.** Decision adoption and ledger-first gets do not compare call hashes.
- **Single Postgres instance** is the only stateful dependency and a single point of failure: while it is down nothing progresses and nothing is lost. **Every guarantee in §1 assumes committed authoritative history is never rolled backward.** A restore that loses committed Tether state is a recovery discontinuity: external effects in the lost interval cannot be reconstructed from the database, and side-effect dispatch must enter an operator-controlled recovery posture before it resumes (OD-4). Guidance: `docs/operations/postgres-recovery.md`.
- **Scale claim.** "Production-shaped correctness at modest, measured scale." No concrete concurrency, latency or throughput limit is claimed until W10.3 measures it (OD-3).
- **Tail truncation and whole-run deletion** of audit chains are not detected (design limit).
- **Mid-run revocation** of a requester is not guaranteed; exposure is bounded by approval expiry and the run deadline.
- **Hosted-provider egress.** Non-sensitive tool output placed in model context leaves the deployment when a hosted provider is used.
- **Data retention** is undecided; nothing is deleted.
