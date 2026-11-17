# Testing

This page explains how to run Tether's test suites, what each one needs, and how to write new tests, including crash and race tests with the chaos harness. It is for engineers who change Tether or add a workload. The evidence behind each guarantee is mapped in [the test strategy](../testing/strategy.md), and the recorded results are in [validation](../validation.md).

## Rules for writing tests

- **Write the test first**, run it, and see it fail for the right reason before you write the code.
- **Never weaken a test to make it pass.** Do not delete an assertion, skip a test, mark it `xfail`, or special-case the code under test. If a test looks wrong, stop and say so.
- **A checker must be able to fail.** A scanner or guard test needs a negative self-test: a case where it is shown to flag a known violation. A check that cannot fail proves nothing.
- **Wait on state, never on time.** Tests poll the database or the database clock, not sleeps.
- **Never add a lint, type or import-contract exception to get green.**

## The suites

Run any task with `uv run poe <task>`. `uv run poe` lists them all.

| Suite | Task | Location | Needs |
|---|---|---|---|
| Lint and format check | `lint` | whole repository | nothing |
| Strict type check | `typecheck` | `src/`, `workloads/` | nothing |
| Import contracts and architecture | `contracts` | `tests/architecture/` | nothing |
| Unit | `test-unit` | `tests/unit/` | nothing |
| Provider contract | `test-provider-contract` | `tests/contract/providers/` | nothing: no network, no database |
| Static bundle | `ci` | the five above | nothing |
| Integration | `test-integration` | `tests/integration/` | Docker and `deploy/compose/.env` |
| Authorization matrix | `test-authz` | `tests/authz/` | Docker and `.env` |
| LangGraph contract | `test-contract` | `tests/contract/langgraph/` | Docker and `.env` |
| Chaos and races | `test-chaos` | `tests/chaos/` | Docker and `.env` |
| Flake gate | `test-flake-gate` | repeats the chaos suite | Docker and `.env` |
| Load and kill harness | `test-perf` | `tests/perf/` | Docker and `.env` |
| Live provider proof | `test-live` | provider contract and one governed run | Docker, `.env`, a paid API key |
| Compose stack | `smoke` | `deploy/compose/docker-compose.yml` | Docker and `.env` |

`uv run poe ci` is what the static CI job runs: lint, type check, contracts, unit tests and the provider contract tests. Run it before every pull request. It needs no database and runs on Windows and Linux.

A recent full run recorded 1,498 passing tests: 588 unit, 89 provider contract, 535 integration, 58 authorization, 11 LangGraph contract, 129 chaos and 23 load-harness tests, plus the architecture tests. Counts change as tests are added. No suite durations are published. CI allows 20 minutes for the integration job (integration, authorization and LangGraph contract together) and 25 minutes for the chaos job.

The `contracts` task runs `lint-imports` and then `pytest tests/architecture`. It stops three kinds of change:

- a workload importing anything from `tether` except `tether.sdk`, or the core importing a workload ([ADR-0002](../adr/0002-core-sdk-workload-layering.md));
- workload-specific names (incident, runbook, demo tool names) appearing anywhere in `src/tether/`, including comments, strings and file names;
- a new top-level `tether.<module>` that is missing from the workload `forbidden_modules` list in `pyproject.toml`, and the use of `now()` in SQL instead of `clock_timestamp()` (database time must come from `clock_timestamp()`).

Never satisfy these by adding `ignore_imports` or by allowing indirect imports.

## Database-backed suites

`test-integration`, `test-authz`, `test-contract`, `test-chaos`, `test-flake-gate` and `test-perf` each start a throwaway PostgreSQL 16 from `deploy/compose/docker-compose.test.yml` (data in tmpfs, published on `127.0.0.1:55432`) and then run pytest. The poe tasks load `deploy/compose/.env`, so create it first:

```bash
cp deploy/compose/.env.example deploy/compose/.env
```

Commands on this page are written for bash. In PowerShell, use `Copy-Item deploy/compose/.env.example deploy/compose/.env`, `$env:NAME = "value"` to set a variable, and a backtick to continue a line.

The test database needs only three variables from that file: `TETHER_POSTGRES_PASSWORD`, `TETHER_MIGRATOR_DB_PASSWORD` and `TETHER_APP_DB_PASSWORD`. Any values will do. The test session defaults `TETHER_ENV` to `test` (an existing value wins) and generates random audit and binding keys itself. `TETHER_TEST_PG_HOST` and `TETHER_TEST_PG_PORT` override the host and port (defaults `127.0.0.1` and `55432`).

How a test gets its database:

1. Once per session, a migrated **template database** is built (`tether_template`: the `tether` schema plus the LangGraph saver).
2. Each test that asks for the `fresh_db` fixture gets its own database cloned from the template (`CREATE DATABASE ... TEMPLATE`), dropped afterwards. Tests are isolated from each other.
3. The `seeded` fixture adds two workspaces, `ws-a` and `ws-b`, each with an agent and one queued run, so cross-workspace behaviour is tested against real rows.

The fixtures live in `tests/integration/conftest.py`; the chaos and authorization suites import them. Shared fixtures in `tests/conftest.py` give tests in-memory spans (`spans`), metric deltas (`metric_values`) and the production JSON log pipeline (`log_lines`, `raw_log`).

**Run one database-backed suite at a time.** Every run on a machine shares one test PostgreSQL and one template database name, and a second session would drop it. This includes other git worktrees on the same host. Stop the database with `docker compose -f deploy/compose/docker-compose.test.yml down -v`.

On Windows, psycopg's async driver cannot use the default event loop, so `tests/conftest.py` selects the selector loop. The database-backed suites run in CI on Linux only.

## Where tests go and how they are named

| Directory | Holds |
|---|---|
| `tests/unit/` | Fast tests with no database. |
| `tests/integration/` | Tests against a real database: transactions, routes, the run graph, the worker handlers. |
| `tests/authz/` | The authorization matrix, a second fixture workspace (`fixture-b`), the route-inventory guard, access events. |
| `tests/chaos/` | Crash, race and process-kill tests. |
| `tests/perf/` | The load and kill harness. |
| `tests/architecture/` | Import contracts and repository-wide scanners. |
| `tests/contract/providers/`, `tests/contract/langgraph/` | The provider contract and the LangGraph semantics contract. |
| `tests/support/` | Shared helpers, such as the fake GitHub transport. |

Files are `test_*.py`. Names describe the behaviour: `test_a_diverged_replay_after_a_crash_fails_closed_without_sending`, not `test_replay_3`. Pytest runs with `--strict-markers`, and three markers are registered: `live`, `chaos` and `perf`. Async tests need `@pytest.mark.asyncio`, because `asyncio_mode` is `strict`.

A test that touches a guarantee belongs in the suite that proves it, and the evidence map in [the test strategy](../testing/strategy.md) is updated in the same change.

## The chaos harness

`tests/chaos/harness.py` injects faults in process. It is a bounded stand-in for killing real processes; one test also does that with real processes (`test_chaos_processes.py`, which starts the API, a worker and the demo target as subprocesses, hard-kills and restarts them).

| Piece | What it does |
|---|---|
| `Crash` | A `BaseException` raised at a named point. Like a killed process, nothing after it runs and every open transaction rolls back. |
| `crash_after_commit(monkeypatch, event_type, skip=0)` | Kills the process right after the transaction that writes the named audit event commits, before the checkpoint, the job completion or the HTTP response. |
| `CrashingProvider`, `GatedProvider` | A scripted provider that crashes on a chosen turn, or that holds each call until released. |
| `TARGET` | A counting target for test write tools: sends, keys, applied effects. `TARGET.script` decides each send in turn: `ok`, `timeout`, `drop` (applied, then the answer is lost), `crash_before`, `crash_after`. A `gate` holds sends in flight. |
| `Env` | The environment. `submit(scenario)`, `approve(...)`, `cancel(...)`, `reconcile(...)`, `worker(provider)` (a fresh worker is a "restarted" or competing one), `drain(*workers)` (process jobs until none are due), `expire_leases()`, `sql(...)`. |
| `assert_reconstructable(run_id)` | After a scenario: the chain is valid and `seq` is contiguous; every ledger entry has its decision and authorization events; dispatch counts equal attempts; every settled entry has an outcome event; terminal status matches the last status event. |
| `assert_terminal_stable(run_id, worker)` | Tries to reopen a terminal run in every way the runtime allows, and checks nothing changed. |

The `env` fixture (in `tests/chaos/conftest.py`) wires an in-process demo target, makes retry backoff immediate, and builds an `Env` over a seeded database. Scenarios are scripted model turns defined in the harness's `SCENARIOS` (`remediate`, `write`, `nonidem`, `strict_read`, `verified`, `cas_stale`).

To add a case:

1. Mark the module `pytestmark = pytest.mark.chaos`.
2. Pick the fault: `crash_after_commit(monkeypatch, "DISPATCHED", skip=0)` for a crash at a commit point, `TARGET.script[:] = ["crash_before"]` for a crash with a write in flight, `CrashingProvider` for a crash during a model call.
3. Drive the run through crashes the way `drive_through_crashes` in `tests/chaos/test_chaos_crashes.py` does: after each crash, close the worker, call `env.expire_leases()`, and start a fresh `env.worker()`.
4. Assert the outcome on authoritative state: the run status, `TARGET.sends` and `len(TARGET.effects)` (the count of real side effects), the ledger rows and the audit events.
5. Finish with `await env.assert_reconstructable(run_id)` and `await env.assert_terminal_stable(run_id, worker)`.

New guarantees add cases here. Do not replace the harness. Races such as an approval against a cancellation are repeated many times in one test (`test_chaos_races.py`) so a rare interleaving shows up.

`uv run poe test-flake-gate` runs the chaos suite repeatedly (20 times by default) and reports every failing test. To choose the count or the target, run the script directly, for example `uv run python scripts/flake_gate.py -n 2 -t tests/chaos/test_chaos_heartbeats.py`. It is manual and not part of CI. The caller must hold the shared test database, as above.

## Load and kill harness

`uv run poe test-perf` runs several in-process workers over many scripted runs, with some unknown outcomes and random crashes, and checks that nothing is stuck, nothing is duplicated and every audit chain verifies. Parameters come from environment variables: `TETHER_PERF_RUNS`, `TETHER_PERF_WORKERS`, `TETHER_PERF_UNKNOWN_FRACTION`, `TETHER_PERF_CRASH_PROBABILITY`, `TETHER_PERF_SEED`, `TETHER_PERF_TIMEOUT_SECONDS`, `TETHER_PERF_RESULTS_DIR` and `TETHER_PERF_SAMPLE`. These are correctness harnesses. They publish no operating limits. `scripts/load_campaign.py` drives the same kind of load against a running Compose stack.

## Live tests

Tests marked `live` call real external systems. They skip without credentials, never run in CI, and are run on request.

**Model provider.** Needs `TETHER_MODEL_PROVIDER=openai`, `TETHER_OPENAI_API_KEY` and `TETHER_OPENAI_MODEL`:

```bash
uv run poe test-live
```

It starts the test database, then runs the live provider contract and one governed incident run on a real model. Calls are paid. [Adding a provider](adding-a-provider.md) explains what they prove.

**GitHub sandbox.** `tests/integration/test_change_control_live.py` creates, labels and closes issues in a repository you name. It needs `CHANGE_CONTROL_GITHUB_REPO`, `CHANGE_CONTROL_GITHUB_TOKEN` and `CHANGE_CONTROL_LIVE_CONFIRM=sandbox`, where the last variable is your statement that the repository is a dedicated sandbox. The token needs issue read and write access to that repository only. There is no poe task. Start the test database, export the three database passwords with the variables above, and run `uv run pytest -m live tests/integration/test_change_control_live.py`. The test closes every issue it opened; it never deletes anything.

## What CI runs

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Runs on | Steps |
|---|---|---|
| `static` | Ubuntu and Windows | `lint`, `typecheck`, `contracts`, `test-unit`, `test-provider-contract` |
| `integration` ("integration + LangGraph contract") | Ubuntu | `test-integration`, `test-authz`, `test-contract` |
| `chaos` ("chaos and races") | Ubuntu | `test-chaos` |

The database jobs generate throwaway passwords in the runner and write them to `deploy/compose/.env`. `test-live`, `test-perf` and `test-flake-gate` never run in CI. CI exercises the OpenAI adapter and the GitHub client against mocked transports only.

## Related

- [Test strategy and evidence map](../testing/strategy.md)
- [Validation](../validation.md)
- [Writing a tool](writing-a-tool.md)
- [Guarantees](../guarantees.md)
