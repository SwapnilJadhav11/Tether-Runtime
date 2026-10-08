# CLAUDE.md — Tether-Runtime

Orientation for Claude Code sessions in this repository. Keep this file short: link to the source documents, don't copy them.

## What Tether is

Tether is a self-hostable, model-agnostic **governed tool-execution layer above LangGraph**. It provides typed tools, deny-by-default policy, identity-aware human approvals, idempotent side-effect handling with an execution ledger, retries and provider fallback, a per-run HMAC audit chain, and run inspection. LangGraph owns graph mechanics: checkpointing, interrupts and replay. Tether owns production semantics and must not reimplement anything LangGraph owns. V1 is proven by a reference workload (incident remediation). The workload is a *client* of Tether, not part of it.

**Tether is not:** a replacement for LangGraph or Temporal; a no-code builder; a hosted SaaS; exactly-once for arbitrary APIs; a compliance or WORM audit product; a prompt-injection detector; a RAG or memory platform; a sandbox for untrusted tool code; a platform-admin HTTP API. V1 has no service principals or machine-triggered runs. Full list: PRD "What We're NOT Building" and plan §14.

## Source documents and precedence

This order is a **working convention for this repository**. The source documents don't declare it themselves. Use it to know which document to read first and which one a change most likely belongs in:

1. `.claude/PRPs/prds/tether-runtime.prd.md`: frozen V1 requirements baseline. Changes go in only as explicit revisions in its Decisions Log.
2. `docs/adr/` (index: `docs/adr/README.md`): ADR-0001 to ADR-0017, all `accepted`.
3. `docs/architecture/tether-v1-architecture.md`: reviewed architecture. §22 lists the invariants, §13.1 the TX1–TX12 transaction catalogue.
4. `.claude/plans/tether-runtime.plan.md`: implementation plan. §3 covers decisions (PQ-\*, PD-\*), §6 the dependency graph, §7 the steps, §9 validation commands and §15 known contradictions.
5. `README.md`: early vision draft. Context only, not authoritative.

If two sources conflict, or code would have to contradict one of them, **stop and surface the conflict to the user**. Never resolve it silently, even in favour of the higher-ranked document.

## Current stage

- **Last completed step: S1.3.** Nothing beyond it is implemented; packages not touched by S1.1–S1.3 are still empty placeholders.
  - S1.1 established the repository, toolchain, CI and T-14 import contracts.
  - S1.2 established the runnable Compose/Postgres foundation: settings, the psycopg pool, `/healthz` and `/readyz`, the idle worker, and `tether migrate`.
  - S1.3 established durable LangGraph checkpointing (exact pins, saver factory on the `langgraph` schema) and the contract suite for sync durability, interrupt/resume, crash recovery and state inspection.
- **Active step: S1.4** (core primitives: canonical JSON, keyed hashes, idempotency keys, SecretsProvider, key ring). Work only on this step. Immediate goal:
  - implement RFC 8785 canonical JSON;
  - implement keyed HMAC call hashes;
  - implement deterministic idempotency keys;
  - implement `SecretsProvider`, `SecretValue`, env-backed secrets, and separate audit and binding key families;
  - enforce float rejection and ±(2^53−1) integer limits for write envelopes;
  - add the property and unit tests the S1.4 plan requires.
- Other steps that the dependency graph would allow are **not** to be started. Don't offer them as alternatives. The active step changes only when the user says so; update this section when it does.

## How to work

- **One plan step per task.** Do only the active step's listed Scope. Don't start the next step in the same task.
- Don't refactor, rename or "improve" anything outside the step's scope. Note it for the user instead.
- Honour the step's "Key decisions honoured" and "Invariants". The recommended defaults in plan §3 (PD-\*) are to be confirmed at their named step: state the default you are applying and ask if unsure.
- **Accepted ADRs are never edited, and never overridden silently, to fit code.** A user approving code that conflicts with an ADR is not enough. If an accepted ADR needs to change:
  1. surface the proposed change and the reason;
  2. get the owner's approval;
  3. write a superseding ADR (`docs/adr/template.md`, next number) and get it accepted;

  all *before* the conflicting code is implemented or merged.
- Don't change the PRD, the architecture doc or the plan as a side effect of implementation work. Propose those changes separately.

## Architecture boundaries (machine-checked)

- `src/tether/` is the core. `tether.sdk` is the **only** module workloads may import. The core never imports `workloads/` ([ADR-0002](docs/adr/0002-core-sdk-workload-layering.md)).
- Nothing workload-specific goes in `src/tether/`: no `incident`, `runbook`, demo tool names, `demo_target` or `ops-demo`, including in comments, strings and file names. This is enforced by `tests/architecture/test_T14_domain_independence.py` (T-14).
- **Adding a top-level `tether.<module>`** means adding it to the workloads `forbidden_modules` list in `pyproject.toml`; the architecture test fails otherwise. Never add `ignore_imports` or allow indirect imports.
- Workloads are discovered through the `tether.workloads` entry-point group, never by static import.

One-line pointers to other load-bearing invariants (details are in the ADRs):

- Postgres is the only stateful dependency: no Redis or broker; one image runs `api` and `worker` ([ADR-0001](docs/adr/0001-two-process-topology-postgres-only.md)).
- Tether tables are authoritative; the LangGraph checkpoint is only a progress cache, run with `durability="sync"` ([ADR-0003](docs/adr/0003-authoritative-tables-checkpoint-progress-cache.md)).
- The job queue is at-least-once and polling-only; wake jobs are never coalesced ([ADR-0004](docs/adr/0004-postgres-job-queue.md)).
- Every run-driver write is protected by an advisory lock and a fencing token ([ADR-0005](docs/adr/0005-run-driver-mutex-and-fencing.md)).
- Only the run driver writes `runs.status`; terminal statuses never change; time comparisons use `clock_timestamp()` ([ADR-0006](docs/adr/0006-run-lifecycle-single-status-writer.md)).
- The ledger reports outcomes honestly: unknown means `indeterminate`, and only a human resolves it ([ADR-0007](docs/adr/0007-execution-ledger-honest-outcomes.md)).
- Idempotency keys are deterministic; re-sends are gated on stop ([ADR-0008](docs/adr/0008-idempotency-keys-and-stop-gated-resends.md)).
- Policy inputs never come from model output; decisions are made only under the current policy version ([ADR-0009](docs/adr/0009-policy-engine-current-policy.md)).
- Approvals bind an RFC 8785 envelope with an HMAC call hash; binding keys are separate from audit keys ([ADR-0010](docs/adr/0010-keyed-canonical-call-hash.md), [ADR-0011](docs/adr/0011-tool-version-pinning-approval-validity.md)).
- When a model turn has several calls, they are governed in sequence and the rest get a typed `skipped` result ([ADR-0012](docs/adr/0012-sequential-multi-call-governance.md)).
- Route-bound audiences; every query is workspace-scoped; out-of-scope returns `404`, never `403` ([ADR-0013](docs/adr/0013-identity-trust-boundary-workspace-isolation.md)).
- No admin HTTP API: platform admin is done through bootstrap YAML and the CLI ([ADR-0014](docs/adr/0014-bootstrap-config-no-admin-api.md)).
- Critical audit events are written inside the state-changing transaction, before side effects; Postgres is the reconstruction record ([ADR-0015](docs/adr/0015-per-run-hmac-audit-and-authoritative-reconstruction.md)).
- History is provider-neutral; fallback never truncates silently ([ADR-0016](docs/adr/0016-provider-neutral-history-and-fallback.md)).
- Sensitive data is masked at a single choke point; unmasked output is never persisted, logged or sent to a model ([ADR-0017](docs/adr/0017-sensitive-data-masking-choke-point.md)).

## Tests first, honestly

- Write the tests in the step's **"Tests to write first"** before the implementation, run them, and **see them fail** for the right reason.
- Negative self-tests must genuinely fail when the violation is introduced. A checker that cannot fail proves nothing.
- Never weaken, skip, `xfail`, special-case or delete an assertion to get green. Never add lint, type or import-contract exceptions to get green. If a test seems wrong, stop and say so.
- Test names embed PRD IDs (e.g. `test_T14_...`). Tests go under `tests/{unit,integration,authz,chaos,perf,architecture,contract}/`.

## Validation

A step is done only when both of these pass:

1. **The non-mutating validation and check tasks currently defined in `pyproject.toml`** under `[tool.poe.tasks]` (lint, type, contract and test checks). Run `uv run poe` to list them; run them with `uv run poe <task>`. Don't run tasks that modify files (e.g. formatters or auto-fixers) as validation. Read the tasks from the file each time; don't rely on a list remembered from earlier.
2. **The active step's "Validation / exit" checks** in plan §7, plus any commands plan §9 lists for that step.

- If the executable config and the plan disagree (a task's scope, a missing task, a different command), **surface the discrepancy**. Don't silently pick one as authoritative or quietly edit either side.
- Don't invent tasks the active step doesn't call for.
- The dev host is Windows (PowerShell); CI also runs on Linux, which is the runtime platform. Keep tasks shell-free and cross-platform.

## Stop and ask instead of inventing

Surface the question to the user, with options, when you hit any of these:

- An open PRD question (PQ-1 to PQ-8 in plan §3.1), or a PD-\* default that doesn't fit the code you're writing.
- A gap or contradiction the plan lists (§15), or a new one between the PRD, ADRs, architecture doc and plan.
- Behaviour no ADR or architecture section covers.
- Schema, constraint, transaction-boundary, lock-order or TX-catalogue semantics.
- Identity, authorization, workspace-scoping, secret-handling or masking behaviour.
- A runtime dependency not listed in PD-10 (or the step's PD items), or a version-pin change.
- Anything that would need a test weakened, a contract exception, or scope from another step.

## Commits

- Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`). Commit messages don't need to contain the step ID.
- **One plan step per PR.** The PR title must start with the step ID, as plan §4 specifies (e.g. `S1.2: …`).
- No AI attribution and no `Co-Authored-By` trailers.
- Never commit secrets, `.env*` files or `tools/dev_issuer/.keys/`. Only public keys belong in bootstrap config.
