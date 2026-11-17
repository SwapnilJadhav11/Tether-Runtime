# Validation

How Tether's claims were checked, and how far that evidence reaches. This page is for a reader
deciding how much to trust the [guarantees](guarantees.md). The full map from each guarantee to the
tests that prove it is the [evidence map](testing/strategy.md); how to run the suites is in
[Testing](development/testing.md).

There are two kinds of evidence. Automated suites run on every push and prove the guarantees under
controlled faults. A one-off live validation ran the system against a real model provider and a
real external API, with crashes and load, once, on one development host.

## Automated suites

On the final validated commit, 1,498 tests passed and none failed.

| Suite | Tests | What it proves | Task |
|---|---|---|---|
| Static checks | — | Lint, strict type checking, import contracts, and architecture tests that keep the core independent of any workload | `lint`, `typecheck`, `contracts` |
| Unit | 588 | Pure logic: canonical JSON and call hashes against known-answer vectors, idempotency keys, policy evaluation, masking, token and API-key validation, audit MACs, retry budgets, the tool contract's validation rules | `test-unit` |
| Provider contract | 89 | The scripted provider and the OpenAI adapter behave the same at the provider boundary, over a mocked transport: no network, no database | `test-provider-contract` |
| Integration | 535 | The governed flow against a real PostgreSQL: every transaction, its audit events and its failure branches; retries, dead-letter, cancellation and deadlines, approvals, reconciliation, verification, replay divergence, masking in every sink, both reference workloads | `test-integration` |
| Authorization matrix | 58 | Every route, audience, role, workspace and credential state, in both directions between two workspaces, plus a guard that fails when a route has no matrix row | `test-authz` |
| LangGraph contract | 11 | The pinned LangGraph version keeps the semantics Tether relies on: synchronous checkpoint durability, interrupt and resume, crash and resume, state inspection | `test-contract` |
| Chaos and races | 129 | Crashes at every critical commit, competing workers, forced lease expiry, repeated races and real process kills (below) | `test-chaos` |
| Load harness | 23 | In-process load and worker-kill scenarios with zero duplicate effects, stuck runs or dead jobs, and the campaign script that drives a running stack | `test-perf` |

Two further tests are live-only: they call the real OpenAI API and skip without credentials.
`test-live` runs them by hand; they never run in CI.

Integration scenarios check the run's audit chain through run inspection when they finish, and
every chaos scenario checks it again (below). A checker that cannot fail proves nothing, so the
route-inventory guard, the sink scan for leaked values and the load harness's self-checks are each
also shown to fail on a planted violation.

## What CI runs on every push

The workflow is `.github/workflows/ci.yml`. Each database-backed job starts a fresh PostgreSQL from
`deploy/compose/docker-compose.test.yml` with throwaway credentials generated in the runner.

| Job | Platform | Runs |
|---|---|---|
| `static` | Linux and Windows | lint, type check, contracts, unit tests, provider contract tests |
| `integration` | Linux | integration tests, the authorization matrix, LangGraph contract tests |
| `chaos` | Linux | the chaos and race suite, once |

CI does not run the load harness, the repeated chaos run or any live test.

## The chaos harness

The chaos suite injects crashes deterministically at named points. A crash is an exception that no
handler catches, so, as with a killed process, nothing after the point runs, every open
transaction rolls back and the job keeps its lease. The main cases crash right after each critical
transaction commits (submission, each policy decision, approval request and decision, each
authorization, dispatch and outcome, and the final status), during a model call, and with a write
in flight. A restarted worker is a fresh runtime with its own connections; competing workers are
several runtimes driven concurrently; a lease can be expired on purpose to let a second worker
take a job whose first worker is still sending. Races such as approve against cancel, cancel
against authorization, and approve against expiry are repeated 30 to 50 times each.

After every scenario two checks run against the authoritative tables. The first reconstructs the
run: the audit chain is valid and contiguous, every ledger entry has its decision and
authorization events, every dispatch has an outcome or is still in flight, every skipped call
names its trigger, and the terminal status matches the last event. The second tries to reopen a
terminal run every way the runtime allows, including a direct status update as the application
role. One further test runs the real `api`, `worker` and demo target as separate processes,
hard-kills them mid-run and restarts them, three cycles of three runs. The harness lives in
`tests/chaos/`; production code contains no fault points.

## Live validation

The following was run once, on one development host, in November 2026. None of it runs in CI, and
none of it is an operating limit.

### Real model provider

The live provider tests passed against the real OpenAI API with model `gpt-4o-mini`: four
provider contract tests and one governed run. Then, on the full Compose stack with three worker
processes and real JWT and API-key credentials, the incident workflow ran end to end:

| Check | Result |
|---|---|
| Model behaviour | Proposed a read, then a restart |
| Approval gate | The restart waited for approval; the target was untouched until then |
| Approver discovery | The approver found the request through the approval list endpoint |
| Separation of duties | The requester's own token was refused on the approval route |
| Binding | The approved call hash equalled the ledger entry's call hash |
| Effect | One restart applied |
| Audit | The chain verified |

### Real external write

A credentialed test against a private GitHub sandbox repository passed. On the full stack, a run
with the real model created exactly one issue, after its approval. The ledger recorded the same
issue number GitHub assigned, and the audit chain verified.

### Crashes around live calls

Every worker process was hard-killed and then restarted at each point.

| Where the crash happened | Cases | Result |
|---|---|---|
| Around model calls: before the first call; between the first call and its read; during the final call | 3 | Every run finished, with one approval and one applied write each |
| Around the external write: before dispatch; twice while the write was in flight | 3 | One issue each. The non-idempotent create was never sent twice. The two interrupted writes were resolved by the tool's read-back as applied, with the issue number as evidence |

### Load on the Compose stack

Three worker processes. Runs were submitted all at once and timed until the whole batch had
drained, so the times are batch drain times, not per-run latency.

| Batch | Result | Time | Effects on the target |
|---|---|---|---|
| 300 scripted runs | All succeeded | 37 s (8.1 runs/s) | Exactly 300 |
| 300 scripted runs, every worker killed and restarted nine times | All succeeded; one approval and two ledger entries per run | — | Exactly 300 |
| 30 runs on the real model | All succeeded; no retries | 33 s (0.9 runs/s) | 30 |

No run was stuck, no job was dead-lettered, and no effect was duplicated.

### Unknown outcomes on the live stack

With the scripted provider, the demo target was frozen or stopped while an approved write was
being sent.

| Target behaviour | What Tether did |
|---|---|
| Frozen during the send, then back | Three sends timed out and the entry became `indeterminate`; the read-back resolved it `verified_applied`: one restart for three sends |
| Stopped | The read-back resolved it `verified_not_applied` |
| Frozen throughout | Verification stopped after three inconclusive probes; the run held in `awaiting_reconciliation`; a separate reconciler identity resolved it with a justification |

### Audit verification

`tether audit verify` reported `VALID` for every run of the validation, 1,120 runs, across both
MAC versions.

## Defects the validation found

Both are fixed, with regression tests.

1. **The development stack could not start.** The Compose `migrate` service did not set
   `TETHER_ENV`, which every Tether process requires.
2. **A read-back concluded "not applied" too early.** GitHub's issue listing lags a create by
   seconds (1.7 to 8.0 s measured). The change-control read-back treated one listing without its
   marker as proof that nothing was applied. It now requires two complete listings 15 s apart;
   anything less is inconclusive and goes to a person. See
   [Unknown outcomes](governance/unknown-outcomes.md).

The second defect is the failure verification is designed to avoid: concluding `not_applied`
without positive evidence. The mocked GitHub transport used in CI has no listing lag, so the
automated suites did not exercise this case before the live run. That is why the settle time is
listed as a limit below.

## Limits of this evidence

- Each live proof ran once, on one host, and is not part of CI. CI exercises the OpenAI adapter
  and the GitHub client against mocked transports only.
- No operating limit (concurrency, latency, throughput, tool duration) is published. The load
  figures above are single-host measurements.
- The chaos suite repeated 20 times in a row passed 19 iterations. The failing iteration hit
  database connection timeouts while the host was suspended for about 12.5 hours; the same test
  passed in the other 19. This is an accepted result with an environmental interruption, not a
  clean 20 of 20.
- No PostgreSQL restore drill has been performed, and recovery objectives are not set.
- The change-control read-back's 15 s settle time is a measurement, not a bound GitHub documents.
- That a read-back only reads is tested against the two reference targets, not provable for
  arbitrary tool code.
- Audit events written before the MAC-version migration do not cover their timestamp. The chain
  detects modification and reordering within a run; detecting deletion of a chain's tail is not
  claimed. The audit log is not WORM storage.
- At-most-once holds for non-idempotent writes; exactly-once is not claimed for arbitrary APIs.
  Idempotent re-sends rely on the target honouring the idempotency key.
- Production runs require a human end-user JWT. Only controlled development and test identities
  exist otherwise.
- The demo target keeps its state in memory.
- A refusal at the pre-dispatch re-check writes no audit event of its own.

## Related

- [Guarantees](guarantees.md)
- [Evidence map](testing/strategy.md)
- [Testing](development/testing.md)
- [Guarantee card](product/tether-v1-guarantees.md)
