# Tether

**A model-agnostic production agent harness for safely executing consequential actions against real systems.**

Tether runs the agent loop, but that is not what it is for. Its job is the integrity boundary between an agent's intent and a real-world side effect:

> The model decides what it wants to do. Tether decides whether, how, and under what guarantees that action is allowed to change the real world.

Every consequential action passes along one governed path: identity → policy → exact-action human approval when required → authorization → dispatch → execution → outcome classification → retry/idempotency decision → verification or human reconciliation when the outcome is unknown → audit and reconstruction. Tether is self-hostable, built on LangGraph for graph mechanics and PostgreSQL as its only stateful dependency.

> **Status (2026-11-16):** V1 is in progress. The baseline commit `ec35254` was hardened and CI-green; most of the remaining V1 implementation has since landed with automated tests (listed below), but none of it has been proven against the real OpenAI or GitHub APIs, under a real-stack load campaign, by the 20× flake gate or by a PostgreSQL restore drill. This README separates **what the guarantee card proves**, **what is implemented and tested but not yet accepted as a guarantee**, **what V1 still requires**, and **what is deferred**. Nothing in the "still requires" or "deferred" lists is current behaviour.

## What Tether guarantees today

The [guarantee card](docs/product/tether-v1-guarantees.md) is the authoritative list, with implementation points, tests and limitations for each entry. In short, at the baseline Tether proves with automated tests that:

- no tool executes without a committed, current-policy authorization record and a committed dispatch record, in that order (G-1);
- a non-idempotent write is sent at most once; an unknown outcome becomes `indeterminate`, is never reported as a failure, and is resolved by an eligible human with a justification, or (since W5.1, for a tool that declares a read-back) by an authorized, stop-gated verification that never re-sends the write (G-2, G-4; the verification part is P-4, below);
- idempotent writes are re-sent only with the same deterministic key, within a bound, and never after a cancellation or deadline (G-3);
- after a stop, no model call starts and no proposal is governed; a dispatched write is never recalled and an unknown outcome is never hidden behind a clean terminal status (G-5, G-6);
- terminal run statuses never change (database-enforced), and at most one fenced run driver changes a run at a time (G-7, G-8);
- a crash of the API or a worker right after any critical transaction commits, during a model call or with an idempotent write in flight loses no run and duplicates no idempotent write against a key-honouring target; hard-killing the real processes mid-run has the same result (G-9);
- invalid credentials fail before any state change; out-of-scope resources return `404`; an approval binds the exact call through a keyed canonical hash, can be decided only by an eligible approver who is not the requester, once, while unexpired, and the worker dispatches only arguments whose hash equals the ledger's (G-11, G-12);
- a replay never adopts a persisted decision, approval or ledger row recorded for a different call: the mismatch is audited as `REPLAY_DIVERGENCE` and the run fails closed after unresolved work drains (G-1, G-8);
- critical audit events (decisions, skipped calls, approvals, dispatches, outcomes, reconciliations, stops, replay divergences, dead-letters and terminal status changes) are written inside the state-changing transaction and chained per run with HMAC, and every event written since migration `0003` also covers its timestamp; tool output and approval arguments pass through one masking function before they are persisted or audited, and secrets, payloads and arguments never reach logs, spans or metric labels (G-13, G-14).

## Implemented and tested, not yet in the guarantee card's §1 (2026-11-16)

These have code and passing automated tests; each enters the card's §1 only when the owner approves its wording (guarantee card §2 and §2.1):

- the TX5 re-check that refuses a changed call, a policy that now denies, or a lapsed approval before dispatch (P-16), and `Sensitive`-field masking through the ledger, approvals, audit, inspection and every other sink (P-17);
- bounded **automatic verification** of unknown outcomes through a tool-declared safe read-back, with human reconciliation as the fallback and the human winning on conflict ([ADR-0019](docs/adr/0019-automatic-verification-of-unknown-outcomes.md), authorized per [ADR-0022](docs/adr/0022-verify-operation-policy-mapping.md); P-4);
- **lease heartbeats** (P-5); a small explicit **repair/denial bound** (P-7); **per-tool approval expiry** with a default and a maximum, values awaiting owner confirmation (P-8);
- the **approval operational API** (list, detail with masked arguments, approve, reject, approver run evidence), the auditor/admin run view, access events, `tether audit verify`, and the T-24 authorization matrix with a second workspace (P-9, P-10);
- provider choice by configuration, bounded stop-gated provider retry and an **OpenAI** adapter, tested against a mocked transport only (P-11);
- a **second, unrelated workload** (repository change control) on the same kernel, tested against a mocked GitHub transport only (P-13);
- fail-closed production configuration: explicit `TETHER_ENV`, a `prod` image target without demo or dev components (P-14).

Known limits (see guarantee card §3): audit events written before migration `0003` keep timestamps outside their MAC; operating limits are not measured; the OpenAI and GitHub integrations have never run against the real services. Migrations `0003` and `0005` must not be rolled out alongside older writers, and downgrading below them is not a production rollback ([production configuration](docs/operations/production-configuration.md) §6).

## What V1 still requires (not yet done)

- A governed run on the **real OpenAI API** and a governed write against a **real GitHub sandbox repository** (both await owner gates).
- **Measured operating limits** from a campaign on a real stack, the 20× chaos flake gate, PostgreSQL recovery objectives with a performed restore drill, and a LangGraph/Temporal/custom-glue comparison.
- The owner decisions listed in the [scope delta](docs/product/tether-v1-scope-delta.md) §3.5.

Full dispositions and owner decisions: [scope delta](docs/product/tether-v1-scope-delta.md). Plan: [`.claude/plans/tether-runtime.plan.md`](.claude/plans/tether-runtime.plan.md) §0.

## Deferred and removed

Deferred past V1: Ollama and automatic live provider failover (the design is kept in ADR-0016), dollar-cost estimates, an approval UI, per-workspace budgets, multi-workspace UX, a cross-run audit-query product, key-lifecycle automation, OIDC/JWKS, production service principals, Cedar/OPA, MCP, Kubernetes. Removed from V1: cross-workspace tool sharing, inline human override. Tether is not a RAG, memory, prompt-management, multi-agent or no-code product.

## How a governed run works

1. A calling app submits a task with a workspace API key and the end user's JWT. Identity is validated before anything else; the agent is resolved server-side from the key's bindings.
2. The run driver asks the model for its next step through a provider-neutral boundary. Tool proposals are validated and evaluated by a deny-by-default policy under the workspace's active policy version: `allow`, `deny` or `require_approval`.
3. `require_approval` pauses the run without holding compute. An eligible approver (never the requester) decides through the approvals API; the approval is bound to the exact call via a keyed canonical hash and expires.
4. Before execution, current policy is re-evaluated; a deny overrides an approval. A ledger entry with a deterministic idempotency key is created, then `dispatched` is committed, then the worker calls the tool.
5. The outcome is `applied`, `not_applied` (positive evidence only) or `unknown`. Unknown becomes `indeterminate`: never a false failure, never a blind re-send of a non-idempotent write. When the tool declares a safe read-back, a bounded, policy-authorized verification may resolve it first; otherwise, or when verification cannot establish the truth, a human reconciles it.
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

Real JWTs for the dev workspace are minted with `tools/dev_issuer/mint.py` (keys live in the git-ignored `tools/dev_issuer/.keys/`); `config/bootstrap/dev.yaml` holds only public keys and API-key hashes. An automated, simulated flow is what the integration, authorization and chaos suites run: `uv run poe test-integration`, `uv run poe test-authz`, `uv run poe test-chaos` (Docker required). The production configuration guide, including the `prod` image target, is in [`docs/operations/production-configuration.md`](docs/operations/production-configuration.md). An OpenAI-driven run is selected with `TETHER_MODEL_PROVIDER=openai` ([model providers](docs/operations/model-providers.md)); it has not yet been run against the real API.

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
