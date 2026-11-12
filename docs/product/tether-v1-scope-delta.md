# Tether V1 Scope Delta and Traceability Matrix

> **Status:** owner-approved dispositions, documentation reconciliation in progress
> **Date:** 2026-11-09
> **Direction record:** [`tether-v1-direction-2026-11.md`](tether-v1-direction-2026-11.md) (decisions `TDR-001`–`TDR-022`, unresolved items `U-001`–`U-011`)
> **Implementation baseline:** commit `ec352540a8fd9f70309b12af53f013b87c2258e4` (branch `hardening/v1-baseline`, CI green on 2026-11-09)
> **Single source:** this file is the only traceability and scope-delta record. The PRD links here instead of repeating the matrix.

## 1. How to read this document

Every material requirement of the frozen PRD, every public claim of the old README and every addition from the direction record has one disposition:

| Disposition | Meaning |
|---|---|
| `KEEP` | The requirement stays in V1 as written (wording may be aligned). It does **not** mean the implementation is complete; see the evidence column. |
| `CHANGE` | The requirement stays in V1 but its wording, scope or mechanism changes. |
| `ADD` | A new V1 requirement introduced by the direction record. |
| `DEFER` | Post-V1. Visible in the roadmap; never described as current capability. |
| `REMOVE` | Out of the V1 product definition. Recorded here so the deletion is traceable. |

The **evidence** column states what the baseline commit proves (`code`, `test`), or `none`. Categories used throughout the repository:

- **implemented now**: present at the baseline with automated evidence;
- **required for V1**: accepted direction, not yet implemented;
- **deferred until post-V1**;
- **removed from scope**;
- **unresolved and awaiting owner approval**.

## 2. Owner decisions recorded on 2026-11-09

These resolve the decisions the reconciliation surfaced (`D-*`) and two unresolved items of the direction record (`U-001`, `U-002`).

| ID | Decision |
|---|---|
| D-1 | The direction record lives at `docs/product/tether-v1-direction-2026-11.md`; `docs/product/` (singular) is the product-document directory. |
| D-2 | The hardening commit `ec35254` is the implementation baseline. The chaos and race suite exists and is run in CI; it must be preserved and extended, not replaced. |
| D-3 | ADR-0019 is drafted as a **complete restatement superseding ADR-0007**: conservative outcome classification and the prohibition on blind re-sends stay; an authorized, bounded, tool-declared read-back may resolve an `indeterminate` outcome when it yields definitive evidence; human reconciliation remains the fallback. ADR-0019 stayed `proposed` until the owner accepted it on 2026-11-09 (OD-1). |
| D-4 | V1 requires **controlled automated simulations and tests**, not production service principals. The production human-JWT trust boundary of ADR-0013 is unchanged. The PRD allows dev/test automation identities restricted by environment, workspace and agent. Production service principals remain post-V1. |
| D-5 | **OpenAI** is the first real hosted provider. Keep provider-neutral history, the `ModelProvider` boundary, bounded provider retry, truthful attempt recording and scripted-provider contract tests. **Defer Ollama and automatic live multi-provider failover** to post-V1. ADR-0016 stays accepted as the future fallback design (see §5, C-3). Fallback is never described as existing. |
| D-6 | The three ADR/code discrepancies are resolved by **code conformance**, as explicit plan steps: (1) replay/adoption call-hash validation and `REPLAY_DIVERGENCE`; (2) audit of typed `skipped` calls per ADR-0012; (3) `created_at` inside a versioned audit-MAC record that still verifies existing chains. No runtime change in the documentation pass. |
| D-7 | Approval UI `DEFER`; cross-workspace tool sharing `REMOVE`; per-workspace cost budgets `DEFER`; inline human override `REMOVE`; multi-workspace UX `DEFER`; workspace isolation and authorization `KEEP`; provider token/usage recording `KEEP`; estimated dollar cost and pricing-table maintenance `DEFER` unless an accepted guarantee needs them. |
| D-8 | Run deadlines stay configurable; the one-hour default is a documented development choice, and seven days is not hard-coded. Per-tool approval expiry is a V1 requirement again, with a safe configurable default and an explicit maximum. |
| D-9 | The plan is revised in place with a re-baseline section; prior step IDs are preserved; one active step at a time; completed baseline work is separated from revised V1 work. |
| D-10 | V1 requires approval list, approval detail, approver-facing run evidence, approve and reject, appropriate masking, `tether audit verify` (or an equivalent supported command) and run-level authoritative reconstruction. A general cross-run audit-query product is deferred unless the guarantee card or operations prove it necessary; the audit data and verification guarantees are not deferred. |
| D-11 | Bounded repair/denial behaviour is a V1 requirement: one understandable bounded counter, or a very small set. The model must not loop on malformed, unknown or repeatedly denied actions. |
| D-12 | `TETHER_ENV` must be explicit; absent or invalid → production-capable processes fail startup. Dev identity shortcuts, the scripted provider, scenario files and demo-only components require an explicit `dev` or `test` environment and are not available in a production build or startup path. |
| U-001 | Real external API: **the GitHub API against a dedicated sandbox repository**. The integration must exercise a real write, approval, audit, an idempotency strategy and verification/read-back without risking an important repository. |
| U-002 | Second workload: **repository change control**, materially different from incident remediation. Candidate tools: inspect repository or issue state; list open change requests; create a change issue; apply a controlled label; close a sandbox issue; verify the resulting issue state. GitHub-specific code stays outside `src/tether/` and uses only `tether.sdk`. |
| Scope | Lease heartbeats, approval list/detail, a real OpenAI provider and PostgreSQL backup/restore/outage/failover guidance are required V1 work. Full signing-key lifecycle automation, OIDC/JWKS, production service principals, Cedar/OPA, Kubernetes and a polished UI remain post-V1. |

### 2.1 Owner decisions on the remaining points (2026-11-09, at acceptance of the reconciliation)

| ID | Decision |
|---|---|
| OD-1 | **ADR-0019 accepted** with the required semantics recorded in its "Acceptance record": optional, tool-declared verification; non-side-effecting read-back; definitive `applied`/`not_applied` may resolve automatically under normal run semantics; inconclusive, verifier error or unavailable verification leave `indeterminate`; human reconciliation is the final fallback; attempts and results are attributable and auditable; cancellation, fencing, idempotency and stop semantics are not weakened. ADR-0007 is `superseded by ADR-0019`. Implementation is W5.1. |
| OD-2 | **Guarantee wording:** "proven" guarantees contain only behaviour supported by the current implementation and tests; planned behaviour stays separately labelled; planned V1 capabilities are not promoted until implementation proves them. |
| OD-3 | **Measured limits:** no invented numbers. The V1 claim is "production-shaped correctness at modest, measured scale"; concrete limits stay `TBD` until W10.3 measures them. |
| OD-4 | **PostgreSQL recovery position:** normal production HA/failover must preserve committed authoritative history; a restore that loses committed Tether state is a *recovery discontinuity*; side-effect guarantees assume committed history is never rolled backward; after a data-loss restore, side-effect dispatch enters an operator-controlled recovery posture before normal dispatch resumes; external effects in the lost interval cannot be reconstructed from the restored database alone; V1 defines the procedure and recommended HA posture and does not claim that arbitrary PITR preserves side-effect guarantees; prefer managed Postgres HA/failover, with backups/PITR as disaster recovery. No mechanism may claim to reconstruct unknowable external actions. |
| OD-5 | **Policy architecture:** stable `PolicyEngine` abstraction; simple but useful built-in V1 implementation; trusted inputs cover requester identity/roles, agent, workspace, tool, required permissions, risk and relevant action context; outcomes stay `allow` / `deny` / `require_approval`; no large custom DSL; OPA/Cedar adapters post-V1. |
| OD-6 | **Tool contract:** may represent effect, risk, required permissions, idempotency support, retry behaviour, execution preconditions, sensitive fields and optional outcome verification; fields are sequenced by the plan, not delivered in one slice. |
| OD-7 | **Approval experience:** V1 requires list, detail, approve and reject; detail exposes enough masked, trusted context to decide without exposing sensitive fields; a frontend inbox is optional polish. The maximum approval-expiry value stays `TBD` until the approval-policy workstream (W4.2) defines its configuration semantics. |
| OD-8 | **Comparison protocol:** Tether vs LangGraph + custom glue vs Temporal + custom glue; same logical workload, same consequential action, same external target where practical, same injected failure points; compare code/glue required, recovery semantics, ambiguous-write behaviour, approval binding, auditability and operator burden; not designed to make Tether win. |
| OD-9 | **GitHub testing:** dedicated sandbox/test repository; least-privilege credentials; no personal or production repositories; deterministic fixtures where practical; documented cleanup/reset; credentials outside Git under production secret-handling rules. |
| OD-10 | **Scope posture reaffirmed:** Tether owns the agent loop; plug-and-play providers required with OpenAI first; side-effect integrity is the differentiator; unrelated workloads and one real external API prove generality; modest measured scale; breadth that does not strengthen the thesis stays deferred or removed. |

## 3. Category summary

### 3.1 Implemented now (baseline `ec35254`, automated evidence)

- Authenticated submission (API key + JWT, route-bound audiences, `401` before any state change) and requester-only run inspection.
- Postgres job queue with `SKIP LOCKED` leases, bounded attempts, backoff, TX12 dead-letter transitions; per-run advisory lock and fencing token.
- LangGraph run graph with `durability="sync"`, TX2 decision adoption, TX3 approvals, TX5 ledger-first authorization with current-policy re-evaluation, wait nodes, TX11 finalize.
- Execution ledger: `dispatched` before any external call; `applied`/`not_applied`/`unknown` classification; `indeterminate`; same-key bounded re-sends for idempotent writes only; stop-gated re-dispatch (TX6r).
- Approval decision (`POST .../{id}/approve|reject`) with core role, policy eligibility and requester ≠ approver; approval expiry timer.
- Cancellation (requester and `workspace_admin`), run deadline timer, stop predicate at every gate; terminal immutability enforced by a database trigger.
- Human reconciliation (`applied` / `not_applied` / `abandon`, justification, `RECONCILED` audit event, stopped runs only terminate); awaiting-reconciliation hold (ADR-0018).
- Per-run HMAC audit chain written inside the state-changing transaction (timestamps not covered by the MAC; see I-3).
- Sensitive-field masking before persistence; log and trace scrubbing.
- OpenTelemetry traces, bounded metrics, structured JSON logs; trace continuity across jobs.
- Bootstrap configuration and CLI (`tether migrate`, `tether bootstrap apply`, `tether apikey new`); policy versions with active-version checks.
- Test evidence: unit, integration, LangGraph contract, architecture (T-14) and the chaos/race suite (C-01–C-07, 50× TX4-vs-TX9 and stop-vs-TX5 races, competing workers, approve-vs-expiry, lost-lock fencing, TX12 for both job kinds, real-process kill) in CI.

### 3.2 Required for V1 (accepted direction, not yet implemented)

Governance kernel: hash-checked create-or-get with `REPLAY_DIVERGENCE` (D-6.1); audited `skipped` calls (D-6.2); versioned MAC record covering `created_at` (D-6.3); bounded repair/denial counter (D-11); per-tool approval expiry with maximum (D-8); governance-aware tool contract incl. verification capability (TDR-009); stable `PolicyEngine` interface with trusted inputs incl. agent identity (TDR-008); lease heartbeats (TDR-010).

Operations surface: approval list, detail, approver run evidence (TDR-006, D-10); `tether audit verify` (D-10); auditor/admin run inspection (ADR-0013 table); access events for inspections.

Outcome verification: ADR-0019 (proposed) and its implementation once accepted (TDR-007).

Proof: real OpenAI provider (TDR-004); GitHub sandbox integration (U-001); repository change-control workload (U-002); authorization matrix T-24 with a second fixture workspace; measured limits and performance suites (TDR-018/019); guarantee card (TDR-015); LangGraph/Temporal comparison (TDR-016).

Production posture: explicit `TETHER_ENV` fail-closed startup, dev/test-only components excluded from production builds (D-12, TDR-020); PostgreSQL recovery documentation and validation (TDR-021).

### 3.3 Deferred until post-V1

Ollama adapter; automatic live multi-provider failover (design kept in ADR-0016); estimated dollar cost and pricing tables; approval UI; per-workspace cost budgets; multi-workspace UX and multiple real workspaces; cross-run audit-query product; full signing-key rotation/retirement automation; OIDC/JWKS; production service principals and machine-triggered production runs; Cedar/OPA adapters; MCP tool sources; Kubernetes/Helm; webhooks/notifications; untrusted-code sandbox; checkpoint encryption at rest; Postgres row-level security; WORM or external audit anchor; data-retention implementation; public cloud demo; a separately deployable governed-execution service.

### 3.4 Removed from the V1 product definition

Cross-workspace tool sharing; inline human override / mid-run instruction injection; "clarification question" and "plan update" model outputs; rate limits and data-scope constraints in the policy engine; "managing tools and permissions" HTTP endpoints; A/B testing of models; general RAG, vector database, long-term memory, prompt management, multi-agent framework, agent marketplace, no-code builder, full authorization-policy language, general chat application, broad framework parity.

### 3.5 Unresolved and awaiting owner approval

| ID | Item | Where it is marked |
|---|---|---|
| ~~ADR-0019 acceptance~~ | **Resolved 2026-11-09 (OD-1): accepted.** Four defaults recorded in the ADR may still be revised before W5.1 | `docs/adr/0019-*.md` |
| U-004 | **Resolved 2026-11-12 (W2.1):** final guarantee wording and evidence links accepted; G-9, G-12, G-13, G-14 and G-16 reworded to the evidence; P-16 and P-17 recorded as implemented-but-unproven evidence gaps | `tether-v1-guarantees.md` (accepted), `docs/testing/strategy.md` §4 |
| U-006 | Measured V1 limits (concurrency, latency, tool duration, lease, database) | PRD success metrics, `docs/testing/strategy.md` |
| U-007 | PostgreSQL RPO/RTO, deployment posture, restore validation | `docs/operations/postgres-recovery.md` |
| U-008 | Exact policy and tool-contract schema | Architecture §6–§7 (gate), plan workstream 3 |
| U-009 | Verification semantics detail beyond ADR-0019's rules | ADR-0019 "Questions for acceptance" |
| U-010 | Approval operational semantics (race, revalidation, masking detail) | Plan workstream 4, PRD approvals |
| U-011 | Comparison protocol | Plan workstream 11 |
| New: GitHub sandbox credentials and test-data strategy | Rules fixed by OD-9 (dedicated sandbox, least privilege, no personal/production repos, deterministic fixtures, documented cleanup, secrets outside Git); the concrete repository, token scope and cleanup procedure are chosen in W9.2 | Plan W9.2 |
| New: approval-expiry maximum value | Deliberately `TBD` until W4.2 defines the configuration semantics (OD-7) | PRD approvals |
| U-007 detail | Concrete RPO/RTO and topology remain `TBD`; the architectural position is fixed by OD-4 | `docs/operations/postgres-recovery.md` |

## 4. Traceability matrix

Line references are to the files as they stood at the start of the reconciliation (`fba66bb` for documents, `ec35254` for code).

### 4.1 PRD requirements

| # | Requirement / claim | Source | Evidence at baseline | TDR | Disposition | Action | ADR impact |
|---|---|---|---|---|---|---|---|
| P-1 | Positioning: layer above orchestration, not a harness | PRD Proposed Solution, Decisions Log "Positioning" | Tether defines and drives the graph (`src/tether/runtime/graph.py`) | TDR-001/002 | CHANGE | Thesis rewritten: complete harness differentiated by side-effect integrity; LangGraph boundary table kept | none (ADR-0003 unchanged) |
| P-2 | Typed registry with risk and required permissions behind `ToolSource` | Core Capabilities row 1 | `ToolDescriptor` has name, version, effect, idempotency, `max_attempts`, `Sensitive` (`src/tether/sdk/tools.py:44-68`); no risk, permissions, preconditions, timeout, `on_exhausted`, verification; no `ToolSource` protocol | TDR-009 | CHANGE | Governance-aware contract incl. verification capability; gap analysis, no parallel schema | none; gate U-008 |
| P-3 | `PolicyEngine` deny-by-default with user, agent, workspace, permissions, risk | Core Capabilities row 2 | Rules by tool + requester roles (`src/tether/policy/engine.py:42-68`, `config.py:38-47`); no agent, permission or risk inputs; no interface class | TDR-008 | CHANGE | Stable interface and trusted-input set; simple built-in kept | none (ADR-0009 kept); gate U-008 |
| P-4 | Approval gate: interrupt, list/approve/reject, hash-bound, per-tool expiry, policy eligibility, re-evaluation | Core Capabilities row 3; authorization table; approval detail | TX3/TX4/TX5 exist; only `POST /{id}/{action}` (`src/tether/api/routers/approvals.py:43`); one global `approval_ttl_seconds` (`settings.py:46`); preconditions always empty (`graph.py:407`) | TDR-006 | KEEP + CHANGE | List/detail/approver evidence required (D-10); per-tool expiry restored with maximum (D-8) | none; gate U-010 |
| P-5 | Durable runs; max duration 7 days | Core Capabilities row 4 | Durable (chaos suite); deadline default 3600 s (`settings.py:38`) | — | KEEP (default changed) | Configurable deadline; one hour documented as the development default (D-8) | none |
| P-6 | Idempotency contract: ledger + deterministic keys | Core Capabilities row 5 | `src/tether/core/hashing.py`; `graph.py:513`; C-05–C-07 | TDR-005 | KEEP | Guarantee G-3, G-4 | none |
| P-7 | Ambiguous writes → `indeterminate`; only a human resolves | Core Capabilities row 6; Reconciliation | Implemented (`execute_tool.py`, `ops.py:63`); no late-result evidence | TDR-007 | CHANGE | Verification-first, human fallback | **ADR-0019 accepted, supersedes ADR-0007** (C-1); implementation W5.1 |
| P-8 | Retries with backoff (tools and models); provider failover; typed failure; per-tool `fail_run` | Core Capabilities row 7 | Tool retries yes; provider retry no; failover no; `fail_run` no | TDR-003/004, D-5 | KEEP (tool retries, provider retry) / DEFER (failover) | Provider retry required; live failover deferred; `fail_run` kept as a descriptor option | ADR-0016 kept (C-3) |
| P-9 | Typed invalid-output handling with repair/denial budget | Core Capabilities row 8 | `CALL_INVALID` typed result (`graph.py:323-339`); only `max_model_turns` 8 | D-11 | KEEP (simplified) | One bounded counter or a very small set | none |
| P-10 | Provider abstraction: Ollama + hosted, config-only switch, neutral history, fallback | Core Capabilities row 9; Provider fallback | `ModelProvider` has `complete()` only (`providers/protocol.py:15`); no adapters; driver defaults to `ScriptedProvider` (`runtime/driver.py:88`); no production provider can load | TDR-003/004, D-5 | CHANGE | OpenAI first; Ollama and live failover DEFER; config-only provider selection required | ADR-0016 kept |
| P-11 | Per-run HMAC audit; critical events write-ahead; queryable by run/user/tool/workspace/approver | Core Capabilities row 10; audit sections | Chain and write-ahead implemented; MAC omits `created_at` (`audit/chain.py:25-49`); no audit query API; no verify CLI | TDR-005/017, D-6, D-10 | KEEP (query DEFER) | Versioned MAC with `created_at`; `tether audit verify` required; broad audit query deferred | ADR-0015 unchanged (C-4) |
| P-12 | Observability and `GET /runs/{id}` timeline | Core Capabilities row 11 | Observability slice done; timeline for requester only (`tasks.py:222`) | TDR-005 | KEEP | Approver and auditor/admin views required | none |
| P-13 | Single lifecycle; cancel cannot recall; unknown never hidden; terminal immutable | Core Capabilities row 12; Run lifecycle | Implemented and chaos-tested | TDR-005 | KEEP | Guarantees G-6, G-7, G-8 | none |
| P-14 | Token usage + estimated cost per run/provider/workspace | Core Capabilities row 13 | `model_calls` rows written (`storage/repos/runs.py:215`); `cost_usd` null; no pricing | D-7 | KEEP (usage) / DEFER (dollar cost) | Usage recording stays; pricing deferred | none |
| P-15 | Three audiences, workspace roles, per-endpoint rules, `404`, access events | Core Capabilities row 14; authorization table | Auth slice done for existing routes; `access_events` table only; no `fixture-b`; no T-24 | TDR-005/019 | KEEP | T-24 and access events required | none |
| P-16 | Reconciliation API and cancellation | Core Capabilities row 15 | Implemented | TDR-005/007 | KEEP + CHANGE | Reconciliation becomes the fallback after verification | ADR-0019 (C-1) |
| P-17 | Every run needs a human end-user JWT; service principals post-V1 | Core Capabilities row 16; Trust model; non-goals; Decisions Log | JWT + API key implemented; dev shortcut isolated (`api/dev_identity.py`) | TDR-014, D-4 | CHANGE | Production boundary unchanged; controlled dev/test automation identities allowed, restricted by environment, workspace and agent | ADR-0013 unchanged (C-2 branch A) |
| P-18 | `SecretsProvider`; sensitive fields masked everywhere | Core Capabilities row 17; Sensitive data | Implemented (`registry/masking.py`, scrubber) | TDR-005 | KEEP | Guarantee G-10 | none |
| P-19 | Worker process; at-least-once queue; `dispatched` before call; bounded delivery | Core Capabilities row 18; Dispatch semantics | Implemented; **no heartbeat** (`queue/postgres.py:10`); lease 60 s, poll 1 s | TDR-010 | KEEP + ADD | Heartbeats required; loss behaviour documented and tested | none (ADR-0004 rule 2 already requires them) |
| P-20 | Workspace enforcement, never cut, proven with `fixture-b` | Core Capabilities row 19; Workspace enforcement | Scoped repositories + composite FKs; `fixture-b` and T-24 missing | TDR-005 | KEEP | Unchanged | none |
| P-21 | Multi-workspace platform experience (Must*, tier-1 slip) | Core Capabilities row 20; Phase 8 | none | D-7 | DEFER | Isolation stays; UX and multiple real workspaces post-V1 | none |
| P-22 | Minimal approval UI (Should) | Core Capabilities row 21 | none | D-7 | DEFER | Optional demo polish | none |
| P-23 | Admin cross-workspace tool sharing (Should) | Core Capabilities row 22 | none | D-7 | REMOVE | Does not strengthen the thesis | none |
| P-24 | Per-workspace cost budgets (Should) | Core Capabilities row 23 | none | D-7 | DEFER | Would need an ADR-0009 addendum | none now |
| P-25 | Inline human override / instruction injection (Should) | Core Capabilities row 24; PQ-7 | none | D-7 | REMOVE | Conflicts with single status writer and stop semantics | none |
| P-26 | Won't (v1): MCP, OIDC, Cedar/OPA, K8s, webhooks, post-exec confirmation, sandbox, eval platform, cloud demo | Core Capabilities row 25; non-goals | — | §5.4/5.5 | KEEP (deferred) | "Post-execution confirmation" reworded: automatic verification is V1 (ADR-0019), a human confirmation step is not | none |
| P-27 | Reference workload: six tools, runbooks, adversarial fixture, `fixture-b`, demo identities | Reference Workload section | Two tools (`workloads/incident_remediation/tools.py`); in-memory demo target; no runbooks | TDR-012/013 | KEEP (reduced) + ADD | Deterministic workload kept and completed as needed by tests; second workload and GitHub integration added | none |
| P-28 | Contract Intelligence as a later second workload | Proposed Solution; Reference Workload; Decisions Log | none | TDR-012, U-002 | CHANGE → ADD | Repository change-control workload is V1 | none (ADR-0002 anticipates it) |
| P-29 | Real external API optional and secondary | Open Questions; Decisions Log "Idempotency demo" | none | TDR-013, U-001 | CHANGE → ADD | GitHub sandbox integration is V1 | none |
| P-30 | Hosted provider open (Anthropic or OpenAI) | Open Questions | none | TDR-004 | CHANGE | OpenAI, user-supplied key, external to source and fixtures | none |
| P-31 | Ollama model; primary/fallback default | Open Questions | none | D-5 | DEFER | Ollama post-V1 | ADR-0016 kept |
| P-32 | Success metrics P-01 < 100 ms, P-02 ~10 runs, P-03 ~100 approvals | Success Metrics | `tests/perf/` empty | TDR-018 | CHANGE | Targets pending measurement; measured limits published (U-006) | none |
| P-33 | Runtime cost ≤ $50/month; delivery in ~4 weeks | Success Metrics | — | TDR-011 | CHANGE | Four-week framing removed; cost ceiling kept as a constraint | none |
| P-34 | Test matrix T-01…T-26, C-01…C-07, P-01…P-03 | Automated test matrix | C-01–C-07 and race cases automated; T-10/T-11/T-24/P-* absent | TDR-019 | KEEP + ADD | Guarantee-to-test matrix; implemented vs intended marked | none |
| P-35 | Footer: frozen, no important ambiguity remains | PRD footer | — | §8.5 | CHANGE | Dated revision status and open gates | none |
| P-36 | Not building: RAG/vector/memory, prompt mgmt, no-code, SaaS, compliance, WORM, injection detection | What We're NOT Building | — | §5.5 | KEEP | Extended with multi-agent framework, marketplace, chat app, full authz language | none |

### 4.2 README claims (early vision draft)

| # | Claim | Disposition | Action |
|---|---|---|---|
| R-1 | "Models are swappable. The runtime is the product." | CHANGE | Idea kept; README leads with the integrity thesis |
| R-2 | RAG search as an example tool | REMOVE | PRD non-goal |
| R-3 | Loop handles "clarification question" and "plan update" outputs | REMOVE | Not in PRD, architecture or code |
| R-4 | Loop ends when "a human aborts or overrides" | CHANGE | Cancel exists; override removed |
| R-5 | Policy enforces rate limits, data scopes, cost budgets | REMOVE / DEFER | Budgets deferred; rate limits never specified |
| R-6 | Context-aware policies depending on state | CHANGE | Policy inputs are trusted identity and registry only (ADR-0009) |
| R-7 | Inline review: override decisions, inject instructions | REMOVE | D-7 |
| R-8 | Post-execution confirmation | CHANGE | Automatic verification plus human reconciliation (ADR-0019), not a human confirmation step |
| R-9 | FastAPI endpoints for "managing tools and permissions" | REMOVE | Contradicts ADR-0014 |
| R-10 | A/B testing; fall back to cheaper models | REMOVE / DEFER | Failover deferred; A/B never specified |
| R-11 | Eval-friendly architecture | CHANGE | Structured run data is exposed; no eval platform |
| R-12 | "Easy to reason about in interviews" | REMOVE | Not a product claim |

### 4.3 Additions from the direction record

| Item | Disposition | Gate / owner |
|---|---|---|
| Guarantee card and guarantee-to-test traceability | ADD | U-004 (wording acceptance) |
| Real OpenAI provider | ADD | — |
| GitHub sandbox integration | ADD | credentials/test-data strategy |
| Repository change-control workload | ADD | — |
| Approval list/detail and approver evidence | ADD | U-010 |
| Tool verification/read-back capability | ADD | ADR-0019 acceptance, U-009 |
| Lease heartbeats | ADD | — |
| Repeatable chaos, race, authorization, integration, performance suites | ADD (chaos/race exist) | U-006 |
| LangGraph/Temporal/custom-glue comparison | ADD | U-011 |
| PostgreSQL backup, restore, outage, failover guidance | ADD | U-007 |
| Measured operating limits | ADD | U-006 |
| ADR/code discrepancy resolution record | ADD | §6 of this file |
| Explicit `TETHER_ENV`, dev/test-only components excluded from production | ADD | — |

## 5. Conflicts with accepted ADRs and their resolution

| ID | Accepted decision | Conflict | Resolution (owner-approved) |
|---|---|---|---|
| C-1 | ADR-0007 rules 7–8: a late result never auto-resolves; only a human resolves `indeterminate`. ADR-0018 and architecture §12.2/§15 build on this. | TDR-007: tool-declared read-back before human reconciliation. | **Resolved: ADR-0019 accepted 2026-11-09** (D-3, OD-1) as a complete restatement; ADR-0007 is `superseded by ADR-0019`. ADR-0018's hold rule is preserved with "resolved" extended to verification outcomes. The design is required and not yet implemented (W5.1). |
| C-2 | ADR-0013 rule 4: every run requires a human end-user JWT. | TDR-014: automated initiation in V1. | **No ADR change** (D-4). Automation uses controlled dev/test identities restricted by environment, workspace and agent; production boundary unchanged; PRD revised. |
| C-3 | ADR-0016 rule 3: retry, then capability-qualified fallback; context cites Ollama + hosted. | D-5 defers Ollama and live failover. | **ADR-0016 kept accepted.** Its decision text defines *how* fallback works when configured; it does not itself require fallback to be implemented in V1 (the requirement came from the PRD, which is now revised). Rules 1, 2, 4, 5, 6 are V1 requirements (TDR-003). Documentation states that fallback is designed, not implemented. |
| C-4 | ADR-0015 rule 1: MAC covers "time"; architecture AD-12 lists `created_at`. | Code omits `created_at` from the MAC. | **Code conformance** (D-6.3): versioned MAC record including `created_at`, existing chains still verify. No ADR change. Guarantee card states the limitation until done. |
| C-5 | ADR-0012 rule 4: each skip is a decision-class audit event (fenced, ADR-0005 rule 3). | Skipped calls are history-only. | **Code conformance** (D-6.2). No ADR change. |
| C-6 | ADR-0003 rule 4 and ADR-0005 rule 6: every create-or-get compares the call hash; divergence fails closed with `REPLAY_DIVERGENCE`. | Adoption and ledger-first gets do not compare; `REPLAY_DIVERGENCE` absent. | **Code conformance** (D-6.1). No ADR change. |
| C-7 | ADR-0004 rule 2 / ADR-0005 rule 5: heartbeats. | Not implemented. | Implementation of the accepted design (TDR-010); no ADR change. |
| C-8 | ADR-0001: Postgres is the single stateful dependency and single point of failure. | TDR-021 recovery story. | No ADR change; operations documentation describes posture and recovery effects. |
| C-9 | ADR-0010 rule 3 / ADR-0014: rotation and retirement are runbooks with a retirement check. | TDR-022 defers automation. | No ADR change; manual procedure documented with its limits. |

No conflict: ADR-0002, 0006, 0008 (ADR-0019 realises its "query instead of re-send" revisit item), 0009, 0011, 0014, 0017, 0018 (see C-1).

## 6. ADR/code discrepancy resolution record (TDR-017, U-005)

| # | Documented invariant | Evidence of the gap at `ec35254` | Resolution | Plan step |
|---|---|---|---|---|
| I-1 | ADR-0003 rule 4, invariant 3: hash-checked create-or-get; `REPLAY_DIVERGENCE` | `govern_call` adopts by dedupe key without comparing (`graph.py:305-314`); ledger-first get without compare (`graph.py:480-482`); approval get without compare (`graph.py:383-385`); `REPLAY_DIVERGENCE` absent from `src/`; the adoption test seeds `call_hash: None` (`tests/integration/test_run_graph.py:235-250`) | Code conformance; negative test proving divergence fails closed | W2-K1 |
| I-2 | ADR-0012 rule 4: skip events audited, fenced | `append_result` writes history only (`graph.py:594-598`) | Code conformance; reconstruction test shows skip events | W2-K2 |
| I-3 | ADR-0015 rule 1 / AD-12: MAC includes time (`created_at`) | `mac_record` has no time field (`audit/chain.py:25-49`); `created_at` is a DB default; tests do not cover timestamps | Code conformance with a versioned record (`mac_version`), verification of v1 chains preserved | W2-K3 |
| I-4 *(found in W2.1; owner disposition 2026-11-12)* | ADR-0006 / ADR-0018 intent: an unresolved real-world side effect is never hidden behind a terminal status | `driver.py` `_fail` marks a run `failed` ("agent is not configured") when its agent is missing from the worker's bootstrap, even if a ledger entry is `dispatched` or `indeterminate`; no test covers the path | Required semantics: (1) no unresolved consequential effect → the run may fail with a typed configuration-unavailable reason; (2) with one, the run is not terminally failed for that reason; (3) no model call, new authorization or external send while the configuration is unavailable; (4) the unresolved state is preserved and the run is held or routed through reconciliation; (5) once every effect is resolved and execution still cannot continue, the run may fail with the explicit reason; (6) inspection and audit keep exposing the unresolved or reconciled action and never let the configuration failure read as "the effect did not occur". Not fixed in W2.1; regression coverage required | W5.2 |

Other documented-but-missing behaviour (not ADR conflicts; see §3.2): lease heartbeats; approval list/detail/approver view; auditor/admin run views; access-event writer; `tether audit verify`; `LATE_RESULT` evidence (superseded by ADR-0019's evidence events); ADR-0011 version-gone-after-dispatch; `on_exhausted=fail_run`; repair/denial counter; per-tool approval expiry; provider selection by configuration; `ModelProvider.capabilities()`; tool descriptor fields (risk, permissions, preconditions, timeout, verification); agent identity as a policy input; second fixture workspace and T-24; performance suites.

## 7. Production/dev separation findings (TDR-020, D-12)

| Finding at `ec35254` | Disposition |
|---|---|
| `env` defaults to `dev` when `TETHER_ENV` is unset (`src/tether/settings.py:26`) | Required V1 change: explicit value or fail startup (D-12) |
| The scripted provider is the driver's default provider (`runtime/driver.py:88`) and refuses only `prod` | Required V1 change: provider selected by configuration; scripted loads only with explicit `dev`/`test` |
| Dev identity shortcut accepted when `TETHER_ENV ∈ {dev, test}` and no `Authorization` header (`api/dev_identity.py`) | Keep the isolation; bind to the explicit environment (D-12) |
| Runtime image ships the reference workload wheel and scenario files (`Dockerfile`) | Required V1 change: production build excludes demo-only components |
| Dev private keys and the raw API key are git-ignored (`tools/dev_issuer/.keys/`); `config/bootstrap/dev.yaml` holds public keys and hashes only | Correct; keep |
| Compose stack sets `TETHER_ENV=dev` explicitly | Correct for the development stack; production example to be added in `docs/operations/production-configuration.md` |

## 8. Document map produced by the reconciliation

| Document | Role |
|---|---|
| `docs/product/tether-v1-direction-2026-11.md` | Accepted product direction (input) |
| `docs/product/tether-v1-scope-delta.md` | This file: dispositions, decisions, conflicts, discrepancy record |
| `docs/product/tether-v1-guarantees.md` | Guarantee card: proven vs planned, with evidence |
| `docs/adr/0019-automatic-verification-of-unknown-outcomes.md` | Accepted ADR superseding ADR-0007 (design required, implementation W5.1) |
| `.claude/PRPs/prds/tether-runtime.prd.md` | Revised requirements baseline (revision 2026-11-09) |
| `docs/architecture/tether-v1-architecture.md` | Architecture with pending-decision markers |
| `.claude/plans/tether-runtime.plan.md` | Re-baselined plan |
| `docs/operations/postgres-recovery.md`, `production-configuration.md`, `key-lifecycle.md` | Operations documentation |
| `docs/testing/strategy.md` | Guarantee-to-test matrix, suites implemented vs intended |
| `CLAUDE.md`, `README.md` | Agent guidance and public front door |
