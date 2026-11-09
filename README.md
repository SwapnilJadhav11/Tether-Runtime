# Tether

**A model-agnostic production agent harness for safely executing consequential actions against real systems.**

Tether runs the agent loop, but that is not what it is for. Its job is the integrity boundary between an agent's intent and a real-world side effect:

> The model decides what it wants to do. Tether decides whether, how, and under what guarantees that action is allowed to change the real world.

Every consequential action passes along one governed path: identity → policy → exact-action human approval when required → authorization → dispatch → execution → outcome classification → retry/idempotency decision → verification or human reconciliation when the outcome is unknown → audit and reconstruction. Tether is self-hostable, built on LangGraph for graph mechanics and PostgreSQL as its only stateful dependency.

> **Status (2026-11-09):** V1 is in progress. The baseline commit `ec35254` is hardened and CI-green but not a finished V1. This README separates **what is proven today**, **what V1 requires**, and **what is deferred**. Nothing in the "required" or "deferred" lists is current behaviour.

## What Tether guarantees today

The [guarantee card](docs/product/tether-v1-guarantees.md) is the authoritative list, with implementation points, tests and limitations for each entry. In short, at the baseline Tether proves with automated tests that:

- no tool executes without a committed, current-policy authorization record and a committed dispatch record, in that order (G-1);
- a non-idempotent write is sent at most once; an unknown outcome becomes `indeterminate`, is never reported as a failure, and is resolved only by an eligible human with a justification (G-2, G-4);
- idempotent writes are re-sent only with the same deterministic key, within a bound, and never after a cancellation or deadline (G-3);
- after a stop, no model call starts and no proposal is governed; a dispatched write is never recalled and an unknown outcome is never hidden behind a clean terminal status (G-5, G-6);
- terminal run statuses never change (database-enforced), and at most one fenced run driver changes a run at a time (G-7, G-8);
- killing the API or a worker at any instruction loses no run and duplicates no supported write (G-9);
- invalid credentials fail before any state change; out-of-scope resources return `404`; approvals bind the exact call through a keyed canonical hash and current policy is re-evaluated before execution (G-11, G-12);
- critical audit events are written inside the state-changing transaction and chained per run with HMAC; sensitive fields are masked before anything is persisted or logged (G-13, G-14).

Known limits of the baseline (see guarantee card §3): no lease heartbeat, no production model provider, approval decision endpoint only, audit timestamps outside the MAC, `TETHER_ENV` defaults to `dev`, operating limits not yet measured.

## What V1 requires (not yet implemented)

- Bounded **automatic verification** of unknown outcomes through a tool-declared safe read-back, with human reconciliation as the fallback, per [ADR-0019](docs/adr/0019-automatic-verification-of-unknown-outcomes.md) (accepted 2026-11-09, supersedes ADR-0007; implementation is plan step W5.1).
- An **approval operational API**: list, detail with masked evidence, approve, reject; approver and auditor run views; `tether audit verify`.
- **Lease heartbeats** so healthy long-running tool calls are neither redelivered nor marked `indeterminate`.
- A real hosted provider (**OpenAI**, user-supplied key) behind the provider boundary; provider choice by configuration.
- A **second, unrelated workload** (repository change control) against a **real external API** (a dedicated GitHub sandbox repository).
- Kernel conformance fixes: hash-checked replay with `REPLAY_DIVERGENCE`, audited skipped calls, a versioned audit MAC covering timestamps.
- Governance-aware tool contracts and a stable policy interface; a small explicit repair/denial bound; per-tool approval expiry with a maximum.
- Fail-closed production configuration: explicit `TETHER_ENV`, dev/test-only components excluded from production.
- Measured operating limits, PostgreSQL recovery guidance, and a LangGraph/Temporal/custom-glue comparison.

Full dispositions and owner decisions: [scope delta](docs/product/tether-v1-scope-delta.md). Plan: [`.claude/plans/tether-runtime.plan.md`](.claude/plans/tether-runtime.plan.md) §0.

## Deferred and removed

Deferred past V1: Ollama and automatic live provider failover (the design is kept in ADR-0016), dollar-cost estimates, an approval UI, per-workspace budgets, multi-workspace UX, a cross-run audit-query product, key-lifecycle automation, OIDC/JWKS, production service principals, Cedar/OPA, MCP, Kubernetes. Removed from V1: cross-workspace tool sharing, inline human override. Tether is not a RAG, memory, prompt-management, multi-agent or no-code product.

## How a governed run works

1. A calling app submits a task with a workspace API key and the end user's JWT. Identity is validated before anything else; the agent is resolved server-side from the key's bindings.
2. The run driver asks the model for its next step through a provider-neutral boundary. Tool proposals are validated and evaluated by a deny-by-default policy under the workspace's active policy version: `allow`, `deny` or `require_approval`.
3. `require_approval` pauses the run without holding compute. An eligible approver (never the requester) decides through the approvals API; the approval is bound to the exact call via a keyed canonical hash and expires.
4. Before execution, current policy is re-evaluated; a deny overrides an approval. A ledger entry with a deterministic idempotency key is created, then `dispatched` is committed, then the worker calls the tool.
5. The outcome is `applied`, `not_applied` (positive evidence only) or `unknown`. Unknown becomes `indeterminate`: never a false failure, never a blind re-send of a non-idempotent write. A human reconciles it (and, once ADR-0019 is implemented, a safe read-back may resolve it first).
6. Cancellation and deadlines stop new actions but never recall a dispatched write. Every step, decision, approval and outcome is reconstructable from PostgreSQL and the per-run HMAC audit chain.

## Quick start (development stack)

The Compose stack is a **development environment** (`TETHER_ENV=dev`): it uses the deterministic scripted model provider and a dev-only identity shortcut, neither of which is available in production.

```bash
# 1. Secrets for the dev stack live in the git-ignored deploy/compose/.env. Generate your own values; never commit them.
#    Required keys: TETHER_POSTGRES_PASSWORD, TETHER_MIGRATOR_DB_PASSWORD, TETHER_APP_DB_PASSWORD,
#                   TETHER_AUDIT_ACTIVE_KEY_ID, TETHER_AUDIT_KEY_<ID>, TETHER_BINDING_ACTIVE_KEY_ID, TETHER_BINDING_KEY_<ID>

# 2. Build and start everything, wait until healthy
uv run poe smoke

# 3. Submit a scripted run and poll it to completion (human-started flow with the dev identity shortcut)
uv run python scripts/smoke_submit.py
```

Real JWTs for the dev workspace are minted with `tools/dev_issuer/mint.py` (keys live in the git-ignored `tools/dev_issuer/.keys/`); `config/bootstrap/dev.yaml` holds only public keys and API-key hashes. An automated, simulated flow is what the integration and chaos suites run: `uv run poe test-integration`, `uv run poe test-chaos` (Docker required). A production configuration guide, including why the current defaults are not production-safe, is in [`docs/operations/production-configuration.md`](docs/operations/production-configuration.md). An OpenAI-driven flow is V1 work, not available at the baseline.

## Documentation

| Topic | Where |
|---|---|
| Product direction (accepted) | [`docs/product/tether-v1-direction-2026-11.md`](docs/product/tether-v1-direction-2026-11.md) |
| Scope delta, traceability, owner decisions | [`docs/product/tether-v1-scope-delta.md`](docs/product/tether-v1-scope-delta.md) |
| Guarantee card (proven vs planned) | [`docs/product/tether-v1-guarantees.md`](docs/product/tether-v1-guarantees.md) |
| Requirements (PRD, revised 2026-11-09) | [`.claude/PRPs/prds/tether-runtime.prd.md`](.claude/PRPs/prds/tether-runtime.prd.md) |
| Architecture decisions | [`docs/adr/README.md`](docs/adr/README.md) |
| Architecture | [`docs/architecture/tether-v1-architecture.md`](docs/architecture/tether-v1-architecture.md) |
| Implementation plan | [`.claude/plans/tether-runtime.plan.md`](.claude/plans/tether-runtime.plan.md) |
| Test strategy and guarantee-to-test matrix | [`docs/testing/strategy.md`](docs/testing/strategy.md) |
| Operations: PostgreSQL recovery, production configuration, key lifecycle | [`docs/operations/`](docs/operations/) |

## Operating limits

Tether V1 targets production-shaped correctness at a modest, measured scale on a single PostgreSQL instance. Limits (concurrency, latency, tool duration, lease and heartbeat behaviour) are not yet measured; the development defaults are listed in the guarantee card §3. While PostgreSQL is unavailable nothing progresses and nothing is lost; backup, restore and failover effects are described in [`docs/operations/postgres-recovery.md`](docs/operations/postgres-recovery.md). Tether does not claim hyperscale, multi-region operation, high availability, or exactly-once execution for APIs outside its idempotency contract.

## Technical stack

Python 3.12 · Pydantic v2 · FastAPI · LangGraph (exact pins; `durability="sync"`) · PostgreSQL 16 (state, ledger, audit chain, job queue, checkpoints) · OpenTelemetry · one image with `api` and `worker` entrypoints · Docker Compose for development.
