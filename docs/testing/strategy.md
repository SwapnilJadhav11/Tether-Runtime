# Test strategy and guarantee-to-test matrix

> **Status:** V1 documentation (direction record TDR-019); suites marked **planned** do not exist yet
> **Baseline:** commit `ec35254` (CI green)

## 1. Suites: implemented vs intended

| Suite | Location | Task (`uv run poe …`) | CI job | Status at baseline |
|---|---|---|---|---|
| Lint, format check | — | `lint` | `static` (Ubuntu + Windows) | implemented |
| Strict typing | — | `typecheck` | `static` | implemented |
| Architecture and import contracts (T-14, package layout, no `now()` in SQL) | `tests/architecture/` | `contracts` | `static` | implemented |
| Unit | `tests/unit/` (20 files) | `test-unit` | `static` | implemented |
| Integration (DB-backed) | `tests/integration/` (17 files) | `test-integration` | `integration` | implemented |
| LangGraph semantics contract (exact pins) | `tests/contract/langgraph/` | `test-contract` | `integration` | implemented |
| Chaos and races | `tests/chaos/` (3 files, 18 tests) | `test-chaos` | `chaos` | implemented (hardening baseline) |
| Compose smoke | `tests/integration/test_compose_smoke.py`; `scripts/smoke_submit.py` | `smoke` | manual | implemented |
| Authorization matrix (T-24, route inventory) | `tests/authz/` | `test-authz` | — | **planned** (directory empty) |
| Performance and load (P-01–P-03) | `tests/perf/` | `test-perf` | — | **planned** (directory empty) |
| Provider contract, live (T-11 on OpenAI) | `tests/contract/providers/` | `test-live` | manual dispatch | **planned** |
| GitHub sandbox integration (T-30) | `tests/integration/github/` or marker `credentialed` | `test-live` | manual dispatch | **planned** |
| Production-build inspection (T-32) | `tests/unit/`, build step | `test-unit` / CI | — | **planned** |

Only the tasks in the first column of "implemented" rows exist in `pyproject.toml`. The plan previously named `test-authz`, `test-perf`, `test-live`, `demo` and `ci`; they are intended, not present.

## 2. How the database-backed suites run

- A throwaway Postgres is started from `deploy/compose/docker-compose.test.yml` (tmpfs). Credentials come from `deploy/compose/.env`, generated per CI job; locally, create the file with the three passwords it names.
- Each test gets its own database cloned from a migrated template (`CREATE DATABASE … TEMPLATE`), so tests are isolated and parallelisable.
- Time comparisons use the database clock; tests wait on database state, never on sleeps.

## 3. The chaos harness (preserve and extend)

`tests/chaos/harness.py` is the owner-approved replacement for the plan's `os._exit` fault points:

- `Crash` is a `BaseException` raised at named points; "crash right after a critical transaction commits" is implemented by observing that transaction's audit event.
- Fresh `WorkerRuntime` instances act as restarted or competing workers; lease expiry is forced; test tools can be gated (block until released) and counted.
- `assert_reconstructable` is the T-12 v0 check (every ledger entry has its decision and authorization events, every dispatch has an outcome or `indeterminate`, `seq` contiguous, chain verifies, status matches the last status event); `assert_terminal_stable` checks terminal immutability.
- One real-process test starts `api`, `worker` and the demo target as subprocesses, hard-kills and restarts them.

Coverage at baseline: C-01–C-07; TX4-vs-TX9 and stop-vs-TX5 races repeated 50×; three workers racing for one run 50×; four workers draining fifty runs; approve-vs-expiry 30×; lost-lock fencing; TX12 for both job kinds; indeterminate → reconciliation. New guarantees must add cases here rather than replace the harness.

## 4. Guarantee-to-test matrix

`G-*` are proven guarantees and `P-*` planned ones from `docs/product/tether-v1-guarantees.md`; `T-*`, `C-*`, `P-0*` are PRD test IDs.

| Guarantee | PRD tests | Existing tests | Missing tests |
|---|---|---|---|
| G-1 authorization and dispatch before any side effect | T-02, T-19, C-04, C-05 | `test_run_graph.py`, `test_slice_run_lifecycle.py`, `test_chaos_crashes.py` | T-19 policy-tightened variant on the real workload |
| G-2 non-idempotent sent at most once | T-17, C-07 | `test_chaos_races.py::test_c07_f7b_*`, `test_chaos_crashes.py::test_c07_*`, `test_retries_dead_letter.py` | — |
| G-3 same-key bounded stop-gated re-sends | T-03, T-20, C-05–C-07 | `test_retries_dead_letter.py`, `test_tool_retry_budget.py`, `test_chaos_races.py::test_c07_overlapping_*` | T-20 on `restart_service` end to end |
| G-4 unknown never failure; human resolves | T-17, T-25 | `test_reconciliation.py`, `test_reconciliation_eligibility.py` | T-27 (ADR-0019 accepted; W5.1) |
| G-5 no action after stop | T-25, T-26 | `test_cancellation_deadlines.py`, `test_chaos_races.py` (50× races) | — |
| G-6 no recall; unknown never hidden | T-26 | `test_chaos_races.py::test_a_stop_while_a_write_is_in_flight_*` | — |
| G-7 terminal immutability | T-26 | `test_schema.py`, `harness.assert_terminal_stable` | — |
| G-8 single fenced driver | C-01–C-03 | `test_chaos_races.py::test_three_workers_*`, `::test_a_driver_that_lost_its_lock_*` | — |
| G-9 crash safety | C-01–C-07 | `test_chaos_crashes.py`, `test_chaos_processes.py`, `tests/contract/langgraph/` | 20× flake gate in CI (plan) |
| G-10 exhaustion yields a domain outcome | C-07 | `test_retries_dead_letter.py`, `test_chaos_crashes.py::test_tx12_*` | — |
| G-11 `401` before state; `404` out of scope | T-18, T-19 | `test_authentication.py`, `test_permissions.py`, `test_identity.py`, `test_scope.py` | T-24 matrix with `fixture-b` (P-10) |
| G-12 exact-call approval, current policy | T-05, T-07, T-08, T-15, T-19 | `test_hashing.py`, `test_canonical.py`, `test_run_graph.py`, `test_permissions.py`, `test_chaos_races.py::test_approve_vs_expiry_*` | T-28 list/detail (P-10); per-tool expiry (P-8) |
| G-13 write-ahead per-run chain | T-12, T-13 | `test_audit_chain.py`, `test_schema.py`, `harness.assert_reconstructable` | timestamp tamper case (P-3); skip events (P-2); verify CLI (P-9) |
| G-14 masking | T-16 | `test_masking.py`, `test_secrets.py`, `test_observability.py` | full sink scan across all audiences |
| G-15 active policy version | T-19 | `test_run_graph.py::test_no_decision_is_made_under_a_stale_policy_version`, `test_bootstrap_apply.py` | API `503` case |
| G-16 workspace scoping | T-24 | `test_scope.py`, `test_uow.py`, `test_schema.py` | second fixture workspace |
| P-1 replay divergence | — | — (adoption test seeds `call_hash: None`) | divergence fails closed; hash-checked adoption |
| P-4 verification | T-27 | — | gate matrix, evidence masking, late probe, human wins, no-mutation contract |
| P-5 heartbeats | T-29, C-07 | — | long call with heartbeat; heartbeat loss |
| P-7 repair/denial bound | T-22 | — | malformed/unknown/denied loop stops at the bound |
| P-11 provider portability | T-10, T-11 | `test_scripted_provider.py` | adapter round-trips; OpenAI contract run |
| P-12 GitHub sandbox | T-30 | — | credentialed suite |
| P-13 second workload | T-31, T-14 | `test_T14_domain_independence.py` | end-to-end run of the change-control workload |
| P-14 production fails closed | T-32 | — | startup refusal; image inspection |
| P-15 measured limits | P-01–P-03 | — | benchmark and load suites |

## 5. Credentialed and long-running suites (planned)

- **OpenAI contract (T-11, live T-10 retry):** marker `live`; needs `OPENAI_API_KEY` from the operator's environment; manual-dispatch CI job; spend logged. Never run on pull requests.
- **GitHub sandbox (T-30):** marker `credentialed`; manual dispatch. Rules (OD-9): a dedicated sandbox/test repository used by nothing else; least-privilege credentials scoped to it; never a personal or production repository; deterministic fixtures where practical; a documented cleanup/reset strategy (tests tag and remove what they create); credentials outside Git, handled under the production secret rules (`SecretsProvider`, CI secrets). *The concrete repository, token scope and cleanup procedure are defined in W9.2.*
- **Performance (P-01–P-03):** marker `perf`; nightly or pre-release; results published as the measured operating limits (U-006).
- **Flake gate:** chaos suite repeated 20× before a release claim (plan), not on every push.

## 6. Rules

- Tests are written before the implementation and must fail for the right reason first; negative self-tests must genuinely fail when the violation is introduced.
- No assertion is weakened, skipped or special-cased to get green; no lint, type or import-contract exception is added.
- Test names embed PRD IDs (`test_T24_…`, `test_c07_…`).
- A change to a guarantee changes its tests in the same step and updates the guarantee card.
