# Tether V1 Product Direction and Documentation Reconciliation Record

> **Status:** Accepted product direction; documentation reconciliation required  
> **Decision date:** 2026-11-09  
> **Repository snapshot reviewed:** `SwapnilJadhav11/Tether-Runtime`, branch `main`, commit `c611d9aa702d59610e9b053b9385309982482f04`  
> **Applies to:** Tether V1 product definition, architecture, planning, implementation claims, and validation  
> **Intended audience:** Maintainers, architects, reviewers, and coding agents working in this repository  
> **Primary follow-up:** Reconcile the PRD, ADRs, architecture, README, `CLAUDE.md`, implementation plan, and test/operations documentation against this record

## 1. Purpose

This document records the accepted direction for Tether V1 after reviewing the existing product definition and Claude's codebase audit. It is the input to a documentation-reconciliation pass; it is not, by itself, a claim that the current implementation already satisfies every decision below.

The reconciliation must make the repository tell one coherent story about:

- what Tether is;
- what Tether V1 guarantees;
- what is in and out of V1;
- which existing requirements remain valid;
- which requirements or ADRs must be revised or superseded;
- which implementation gaps must be closed before a claim is made; and
- which questions still require an explicit decision.

This record deliberately distinguishes accepted direction from unresolved design choices. Do not turn an unresolved item into an assumed requirement.

## 2. Authority and change-control rules

Use this document as the latest product-direction input, subject to the following rules:

1. **Do not silently override an accepted ADR.** If this direction conflicts with an accepted ADR, identify the conflict and either:
   - change the implementation to conform to the accepted ADR; or
   - write a new proposed/superseding ADR, preserve the old ADR and its history, and update status and cross-links according to the repository's ADR process.
2. **Do not rewrite history.** Do not edit an old ADR so that it appears to have always contained the new decision. Prefer an explicit supersession trail.
3. **Do not claim unimplemented guarantees.** README and PRD language must distinguish current behavior, V1 requirements, planned work, and post-V1 ideas.
4. **Do not delete requirements merely because they are inconvenient.** A deletion or deferral must be connected to the product thesis and recorded in the scope-delta/traceability matrix.
5. **Do not expand implementation during the documentation pass unless separately authorized.** First reconcile the documents and produce an evidence-backed implementation plan.
6. **Verify audit findings against the current revision.** The audit findings in this record are inputs to investigate, not a substitute for inspecting the current code and tests.
7. **Preserve security boundaries.** Never place API keys, tokens, real credentials, or sensitive production examples in documentation, tests, fixtures, or committed configuration.

If two current documents conflict and neither is clearly superseded, record the conflict as unresolved rather than choosing silently.

## 3. Product thesis

### 3.1 Working definition

> **Tether is a model-agnostic production agent harness focused on safely executing consequential actions against real systems. It owns the agent execution loop, but differentiates through policy-controlled actions, exact-action human approval, idempotent and crash-safe execution, honest ambiguous-outcome handling, automatic verification where possible, human reconciliation where necessary, durable state, attribution, and auditable reconstruction.**

### 3.2 Design principle

> **The model decides what it wants to do. Tether decides whether, how, and under what guarantees that action is allowed to change the real world.**

### 3.3 What Tether owns in V1

Tether remains a complete harness in V1. It owns:

- model invocation through a provider abstraction;
- agent-loop and run-state management;
- tool selection and governed execution;
- policy evaluation;
- approval and authorization checks;
- dispatch and outcome classification;
- retry, idempotency, and ambiguous-outcome behavior;
- reconciliation;
- durable attribution and audit reconstruction; and
- operational APIs needed to use these capabilities.

Owning the agent loop is part of the V1 product. It is not the main differentiator. The differentiator is the integrity boundary between an agent's intent and a real-world side effect.

### 3.4 Product posture

Tether V1 targets **production-shaped correctness at a modest, measured scale**. It does not claim internet-scale, multi-region infrastructure or feature parity with broad agent frameworks and workflow engines.

The intended proof is that the same execution/governance kernel works across unrelated workloads and against at least one real external API, while remaining explicit and honest about its operating limits.

## 4. Accepted decisions

The identifiers below should be used in the PRD, plan, ADR proposals, and traceability matrix.

### TDR-001 — Tether owns the agent loop in V1

**Decision:** Keep Tether as a complete agent harness rather than reducing V1 to a standalone governed-tool service.

**Consequence:** The architecture and PRD must cover model calls, loop state, tool calls, governance, execution, and outcomes as one system. A separately deployable governed-execution boundary may be explored later, but it is not the V1 product definition.

### TDR-002 — Side-effect integrity is the primary differentiation

**Decision:** Position Tether around safe, attributable, policy-controlled real-world action, not around having invented another generic agent loop.

**Consequence:** Requirements and milestones that do not materially strengthen this thesis should be re-triaged. README and demo narratives must lead with verifiable execution guarantees rather than broad framework breadth.

### TDR-003 — The model layer must be plug-and-play

**Decision:** Model agnosticism is a real V1 product requirement, not merely a test seam.

**Consequence:** Provider-specific behavior must be isolated behind a stable adapter/interface. Runtime governance and tool semantics must not depend on OpenAI-specific response shapes outside the adapter boundary.

### TDR-004 — Prove the abstraction with a real hosted model

**Decision:** V1 must support at least one real hosted model provider. OpenAI is the first provider and will be exercised using a user-supplied API key.

**Consequence:** Secret injection must remain external to committed source and fixtures. A large provider catalogue and elaborate provider-failover behavior are not required to prove V1 model portability.

### TDR-005 — Tether owns the complete consequential-action integrity chain

**Decision:** The governed path is:

```text
identity
  -> policy
  -> exact-action approval when required
  -> authorization
  -> dispatch
  -> side-effect execution
  -> outcome classification
  -> retry/idempotency decision
  -> reconciliation when necessary
  -> audit/reconstruction
```

**Consequence:** Architecture, threat model, tests, and audit records must cover this path end to end. An approval alone is not sufficient if dispatch, retries, or audit can bypass the decision.

### TDR-006 — V1 needs a complete approval API; a polished UI is optional

**Decision:** V1 must support, at minimum:

- listing pending approvals;
- viewing approval details;
- approving an action; and
- rejecting an action.

The detail response should expose appropriately masked evidence such as the requester, agent/run, exact action, arguments, expiry, risk/context, and related run information. Sensitive values must remain redacted.

**Consequence:** An endpoint that works only when an operator already knows an approval ID is insufficient. A graphical approval inbox is optional demo polish and is not a V1 release requirement.

### TDR-007 — Unknown outcomes use automatic verification first when supported

**Decision:** A tool may declare a safe read-back/verification capability. When the outcome of a consequential action is unknown, Tether should try that capability before requiring a person to reconcile the result.

```text
unknown outcome
  -> verification supported?
       -> yes: perform safe read-back
            -> definitive result: resolve automatically and audit evidence
            -> still ambiguous: require human reconciliation
       -> no: require human reconciliation
```

**Consequence:** Verification must not resend the original write. It must be separately authorized as appropriate, bounded, attributable, and auditable. Human reconciliation remains the safe fallback.

### TDR-008 — Use a stable policy interface with a simple built-in engine

**Decision:** V1 provides a stable `PolicyEngine` interface and a meaningful but deliberately simple built-in implementation.

Trusted policy inputs should include, where relevant:

- requester identity and roles;
- agent identity;
- workspace or tenant context;
- tool and requested action;
- required permissions;
- risk classification; and
- relevant resource/action context.

The required decision set remains small:

```text
ALLOW
DENY
REQUIRE_APPROVAL
```

**Consequence:** Do not build a full authorization language in V1. OPA and Cedar are potential post-V1 adapters behind the same interface.

### TDR-009 — Tool contracts carry governance semantics

**Decision:** Tool definitions must be rich enough to support the execution guarantees rather than merely describe callable parameters.

The contract should be able to represent, as applicable:

- effect classification (read/write and consequence);
- risk;
- required permissions;
- idempotency support and keying expectations;
- retry behavior;
- execution preconditions;
- sensitive fields and masking rules; and
- optional reconciliation/read-back capability.

**Consequence:** The exact schema remains an architecture decision, but these concepts must be traceable from policy and approval through execution and audit.

### TDR-010 — Long-running tool calls require lease heartbeats

**Decision:** V1 must support safe lease renewal/heartbeats for legitimate long-running tool calls.

**Consequence:** A healthy operation must not become abandoned or indeterminate merely because it runs longer than a fixed worker lease. Heartbeat ownership, loss behavior, timeouts, and recovery must be documented and tested.

### TDR-011 — The original V1 scope may be reduced explicitly

**Decision:** Previously planned features may be changed, deferred, or removed when they do not strengthen Tether's product thesis or proof.

**Consequence:** Every change must be explicit in the reconciliation matrix. Do not preserve low-value breadth solely because it appeared in an older PRD, and do not silently delete it.

### TDR-012 — Generality must be proven with unrelated workloads

**Decision:** Incident remediation alone is not sufficient evidence of a general-purpose harness. V1 validation must exercise substantially different tools, data, and actions through the same execution/governance kernel.

**Consequence:** Select at least one additional workload whose action and failure characteristics differ materially from incident remediation. Avoid workload-specific logic in the core runtime.

### TDR-013 — V1 must govern at least one real external API

**Decision:** The deterministic demo/chaos service remains useful, but V1 must also demonstrate governed action against at least one real external system.

**Consequence:** The exact external API is unresolved. Its test or sandbox mode must be safe and repeatable, and the integration must exercise the same approval, idempotency, outcome, reconciliation, and audit semantics claimed by the core.

### TDR-014 — Support both human-started and automated operation

**Decision:** V1 must support:

- human-started runs for realistic manual testing and approval demonstrations; and
- automated initiation for deterministic simulations, repeatable tests, and unattended scenarios.

**Consequence:** Production-grade service-principal scope remains unresolved. The docs must not imply autonomous production operation if automated scenarios only use controlled test identities.

### TDR-015 — Write a guarantee card before expanding feature work

**Decision:** Produce a short, evidence-backed guarantee card containing only guarantees the implementation proves.

Each guarantee must map to:

- a precise statement and boundary conditions;
- implementation points;
- automated tests or other repeatable evidence;
- failure/ambiguity behavior; and
- known limitations.

Example form, subject to code and test verification:

> Once a non-idempotent write has been dispatched, Tether will not automatically resend it after an ambiguous outcome.

**Consequence:** The guarantee card becomes the spine of the README, PRD acceptance criteria, architecture review, and test plan. Aspirational behavior belongs in the plan, not in the current-guarantees section.

### TDR-016 — Validate differentiation against custom glue

**Decision:** Compare equivalent governed flows built with:

- LangGraph plus custom glue;
- Temporal plus custom glue; and
- Tether.

Exercise failures around approval, dispatch, crash, redelivery, unknown outcomes, cancellation, and recovery.

**Consequence:** The comparison is a product-learning exercise, not a marketing benchmark designed to guarantee a Tether win. If Tether does not remove meaningful production complexity, revise the thesis or design.

### TDR-017 — ADR/code discrepancies must be resolved explicitly

**Decision:** For every conflict between documented guarantees and code, either fix the implementation or revise/supersede the requirement through the repository's decision process.

**Consequence:** Contradictions may not remain hidden behind vague wording. The audit reported three specific items that require current-code verification:

1. replay/adoption hash validation;
2. skipped-call audit behavior; and
3. whether `created_at` is covered by the audit MAC as documented.

The choice for each item—implementation fix or superseding decision—has not yet been made.

### TDR-018 — V1 claims correctness at modest, measured scale

**Decision:** V1 does not claim hyperscale or multi-region infrastructure. It claims production-shaped semantics and correctness at a measured scale.

**Consequence:** Establish and publish honest test conditions and operating limits, including concurrency, worker behavior, polling/latency, tool duration, and database assumptions. Exact targets remain unresolved until measured.

### TDR-019 — Reliability proof must be repeatable and automated

**Decision:** Manual experiments are useful evidence but are not sufficient for V1.

The validation program should include, as relevant:

- race/concurrency tests;
- crash and chaos tests;
- authorization-matrix tests;
- integration and external-API contract tests;
- model/provider and agent-framework contract tests;
- reconciliation tests;
- audit-integrity tests; and
- performance/soak tests at the stated V1 scale.

**Consequence:** CI must run an appropriate safe subset, with longer or credentialed suites clearly documented and reproducible.

### TDR-020 — Production operation must exclude dev-only shortcuts

**Decision:** Development identity bypasses, demo scenarios, and test-only mechanisms must be clearly separated from production operation and must not be accidentally enabled by a weak configuration toggle.

**Consequence:** Review build contents, startup configuration, authentication defaults, and deployment examples. Production documentation must fail closed and describe the controls that prevent dev-only behavior.

### TDR-021 — Define a PostgreSQL backup and failover story

**Decision:** PostgreSQL is an authoritative state dependency and requires an explicit V1 operations story. Tether need not invent its own database HA system.

Document:

- authoritative data and what must be backed up;
- behavior during database unavailability;
- backup and restore procedure;
- recovery validation;
- recommended HA deployment posture;
- acceptable data-loss and downtime assumptions; and
- the effect of failover/restore on leases, dispatch records, approvals, reconciliation, and audit continuity.

**Consequence:** Exact RPO/RTO values and supported deployment topology remain to be decided and validated.

### TDR-022 — Full signing-key lifecycle automation is post-V1

**Decision:** Existing key IDs and security mechanics remain relevant, but complete operational tooling for key rotation and retirement does not block V1.

**Consequence:** Document the current limitation and future hardening work. Do not claim seamless rotation/retirement if it is not implemented.

## 5. V1 scope delta

This section describes the intended disposition. The documentation reconciliation must map every existing PRD requirement and plan item to one of these categories with exact file references.

### 5.1 KEEP

Keep and strengthen capabilities that directly support the thesis:

- ownership of the model/agent loop;
- durable run and action state;
- policy decisions and authorization checks;
- exact-action approval binding;
- idempotency and controlled retry behavior;
- explicit ambiguous/indeterminate outcomes;
- cancellation and recovery semantics;
- attribution and audit reconstruction;
- PostgreSQL as the authoritative V1 state dependency;
- deterministic failure/chaos testing; and
- human-started governed actions.

`KEEP` means preserve the requirement after confirming its wording matches the accepted direction. It does not imply that the current implementation is complete.

### 5.2 CHANGE

Revise existing material where needed to reflect:

- product positioning centered on consequential-action integrity;
- a real, model-agnostic provider boundary with OpenAI first;
- an operational approval list/detail/action API instead of ID-only actions;
- automatic outcome verification followed by human reconciliation;
- a stable policy interface instead of an expanding home-grown policy language;
- governance-aware tool contracts;
- heartbeat-safe long-running work;
- production-shaped correctness at a measured, modest scale; and
- explicit separation of development/demo and production behavior.

### 5.3 ADD

Add requirements, design coverage, plans, and evidence for:

- the guarantee card and guarantee-to-test traceability;
- one real hosted model provider;
- at least one real external API;
- at least one unrelated second workload;
- approval inbox/list and detail APIs;
- tool verification/read-back capability;
- lease heartbeats;
- repeatable chaos, race, authorization, integration, and performance tests;
- the LangGraph/Temporal/custom-glue comparison;
- PostgreSQL backup, restore, outage, and failover guidance;
- measured operating limits; and
- an explicit ADR/code discrepancy resolution record.

### 5.4 DEFER TO POST-V1

Unless a separately accepted decision says otherwise, defer:

- a polished approval frontend;
- OPA and Cedar adapters;
- a sophisticated custom authorization DSL;
- complete signing-key rotation/retirement automation and runbooks;
- a large model-provider catalogue;
- hyperscale or multi-region claims;
- a general evaluation platform; and
- a separately deployable governed-execution service extracted from the harness.

Deferral must be visible in the PRD/roadmap and must not be described as current capability.

### 5.5 REMOVE FROM THE CORE PRODUCT DEFINITION

Tether V1 is not being designed as a:

- general RAG platform;
- vector database;
- long-term memory product;
- prompt-management platform;
- multi-agent framework;
- agent marketplace;
- no-code workflow builder;
- full authorization-policy language;
- general chat application; or
- broad substitute for every feature in existing agent/workflow frameworks.

Integration with these systems may be possible, but building them is not part of the V1 thesis.

### 5.6 RE-TRIAGE; DO NOT DELETE WITHOUT EXPLICIT MAPPING

Claude's audit questioned whether the following previously discussed features strengthen the core thesis:

- multi-workspace user experience beyond the minimum isolation needed for correctness;
- cross-workspace sharing;
- cost budgets;
- inline policy override mechanisms; and
- elaborate provider fallback.

The accepted decision is to shrink irrelevant scope, not to assume that every item above is deleted. For each existing requirement, classify it as `KEEP`, `CHANGE`, `ADD`, `DEFER`, or `REMOVE`, explain why, and obtain approval for any consequential removal or changed security behavior.

## 6. Unresolved decisions and required design choices

These items must remain clearly labeled until decided. They are not permission for an agent to invent a product decision.

| ID | Unresolved item | Required output or decision gate |
|---|---|---|
| U-001 | Real external API | Select a safe sandbox/test target (examples raised included GitHub, Kubernetes, or Stripe test mode) based on how well it exercises the guarantees. Record rationale and credentials/test-data strategy. |
| U-002 | Second unrelated workload | Select a workload materially different from incident remediation and define what runtime reuse demonstrates generality. |
| U-003 | Automated identity depth | Decide whether V1 needs production-grade service principals/machine identities or only controlled machine/test identities for automated scenarios. Ensure product claims match the choice. |
| U-004 | Exact guarantee card | Review code/tests and approve the final guarantee wording, preconditions, exclusions, and evidence links. |
| U-005 | ADR/code mismatches | For replay/adoption hash validation, skipped-call audit behavior, and `created_at` MAC coverage, choose implementation conformance or an explicit superseding decision. |
| U-006 | Measured V1 limits | Establish concurrency, throughput/latency, maximum/typical tool duration, worker/lease, and database test targets based on repeatable measurement. |
| U-007 | PostgreSQL recovery objectives | Define supported deployment posture, RPO/RTO expectations, restore validation, and failover assumptions. |
| U-008 | Policy and tool contract schema | Specify the stable interfaces, trusted input construction, versioning, masking, and compatibility rules without creating an oversized DSL. |
| U-009 | Verification semantics | Define how tools report verification support, authorization of probes, evidence captured, bounded retry/polling, and the transition to human reconciliation. |
| U-010 | Approval operational semantics | Confirm expiry, race/concurrency, reauthorization/revalidation, actor permissions, masking, and approve/reject audit behavior. |
| U-011 | Comparison protocol | Define fair LangGraph, Temporal, and Tether flows, failure injection, measurements, and conclusions that would validate or weaken the thesis. |

When documentation can proceed without one of these answers, insert a clearly marked decision gate or `TBD` with an owner. Do not fill the gap with a hidden assumption.

## 7. Audit findings to verify against the current repository

Claude's audit motivated this direction and reportedly found the following. The reconciliation agent must locate current code/tests/docs for each item and record whether it is still accurate.

| Area | Reported finding | Reconciliation requirement |
|---|---|---|
| Core strength | Consequential-action execution/governance is the strongest part of the implementation. | Preserve and make it the product spine; link claims to evidence. |
| ADR mismatch | Replay/adoption hash validation may not match its documented invariant. | Trace the code path and test coverage; propose implementation or ADR resolution. |
| ADR mismatch | Skipped calls may not be audited as documented. | Verify exact behavior and close or formally supersede the guarantee. |
| Audit integrity | `created_at` may not be covered by the audit MAC despite documentation saying it is. | Verify serialization/MAC inputs and tests; resolve the contradiction explicitly. |
| Worker leases | Long-running calls reportedly lack lease heartbeats. | Add architecture and plan coverage for TDR-010; do not claim safe long calls until proven. |
| Approvals | Approval operations may lack a usable list/detail workflow. | Reconcile APIs and requirements with TDR-006. |
| Reconciliation | Indeterminate outcomes may rely only on manual resolution. | Reconcile tool contracts and state transitions with TDR-007. |
| Policy | Current YAML/built-in policy was characterized as thin. | Preserve simplicity while defining the stable interface and trusted inputs in TDR-008. |
| Identity | Human-JWT-only execution may contradict autonomous/alert-triggered examples. | Resolve U-003 and correct claims/examples. |
| Scale | One job per worker, polling, no heartbeat, and a single PostgreSQL dependency were raised as limits. | Measure and publish limits; add heartbeat and database operations work without claiming hyperscale. |
| Security hygiene | A development environment toggle may enable a dev identity shortcut, and demo/test artifacts may ship in production builds. | Inspect defaults and build contents; document and plan fail-closed separation. |
| Key lifecycle | Key IDs exist but rotation/retirement operations may be incomplete. | State the post-V1 limitation accurately. |
| Proof | Important reliability experiments may be manual rather than automated. | Convert accepted guarantees into repeatable tests and CI evidence. |
| Real-world proof | The current demo may depend on controlled/in-memory systems rather than a real provider and external API. | Add TDR-004 and TDR-013 proof without removing deterministic chaos coverage. |

## 8. Repository-specific reconciliation map

This section is grounded in the `main`-branch snapshot identified in the header. Re-check it if the branch advances before reconciliation.

### 8.1 Current governing sources

The repository already has a clear document hierarchy, but it predates this direction record:

| Role | Current file | Current condition |
|---|---|---|
| Frozen V1 requirements | `.claude/PRPs/prds/tether-runtime.prd.md` | Detailed and internally consistent with the October architecture, but several decisions are now superseded at the product-direction level. Its Decisions Log is the proper place for explicit revisions. |
| Accepted decisions | `docs/adr/README.md` and `docs/adr/0001` through `0018` | All 18 ADRs are listed as accepted. They must not be silently edited to fit this record. |
| Reviewed architecture | `docs/architecture/tether-v1-architecture.md` | Matches the frozen PRD and accepted ADRs; it must be revised only after conflicts are resolved through the ADR process. |
| Implementation plan | `.claude/plans/tether-runtime.plan.md` | Contains the original M1–M9 sequence and traceability. The implementation has proceeded through owner-approved accelerated slices that are recorded in `CLAUDE.md`, so the plan and actual sequencing have diverged. |
| Coding-agent guidance and live status | `CLAUDE.md` | Correctly requires conflicts to be surfaced and accepted ADRs to be superseded rather than rewritten. Its product summary, precedence list, active-step rules, and carried-forward scope must be updated for the new direction. |
| Public introduction | `README.md` | An early vision draft. It is explicitly non-authoritative and is stale relative to both the current code and the new product direction. |

The reconciliation should add this file to the source order as the latest accepted **product-direction input**. It does not automatically outrank an accepted ADR at the implementation-decision level; a conflict still requires the ADR process.

### 8.2 Direct conflicts requiring explicit resolution

| Conflict | Existing source | New direction | Required action |
|---|---|---|---|
| Human-only versus automatic verification | ADR-0007 rules 7–8 say a late result never auto-resolves and only a human resolves `indeterminate`; ADR-0018 and architecture §§12/15 assume that flow. | TDR-007 requires tool-declared safe read-back before human reconciliation. | Draft the next ADR (currently ADR-0019) to define automatic verification and its evidence/authorization semantics and to state exactly which ADR-0007 rules it supersedes or refines. Assess consequences for ADR-0018. Do not change runtime behavior until it is accepted. |
| Human-JWT-only versus automated initiation | PRD non-goals and Decisions Log, plan §14, and `CLAUDE.md` say machine-triggered runs/service principals are post-V1 and every run requires a human JWT. | TDR-014 requires automated initiation as well as human-started runs; U-003 leaves the production-grade identity depth open. | Revise the PRD explicitly. Decide whether controlled test/machine identities are enough or whether a service-principal model is V1. Create/supersede identity ADR material if the trust boundary changes. |
| External integration optional versus required | PRD Open Questions and Decisions Log make a real external API optional/secondary; plan §14 places it post-V1. | TDR-013 makes one real external API mandatory V1 proof. | Move the selection and implementation into the V1 PRD/plan, with a decision gate for U-001. Preserve `demo-target-service` for deterministic chaos testing. |
| Second workload later versus required | The PRD names Contract Intelligence as later; plan S9.1 is a Tier-1 slip candidate and §14 places the second workload post-V1. | TDR-012 requires at least one unrelated workload in V1. | Promote a minimal second SDK-only workload into the V1 critical proof and resolve U-002. It does not have to include broad multi-workspace UX. |
| Provider fallback scope | The PRD requires Ollama plus one hosted provider and fallback; ADR-0016 makes capability-qualified per-turn fallback accepted; plan S6.2/S6.3 implement fallback and two live adapters. | TDR-003/TDR-004 require provider portability and OpenAI first, while broad catalogue/fallback work is not itself the product thesis. | Do not infer that ADR-0016 is cancelled. Decide whether its fallback requirement remains V1, is simplified, or needs a superseding ADR. OpenAI is selected as the first hosted adapter; Ollama and live fallback need explicit disposition. |
| Key-lifecycle work | Plan S8.1 includes key-rotation runbooks and `keys check-retirement`. | TDR-022 defers full rotation/retirement automation. | Distinguish minimal safety needed by current keyed records from full lifecycle automation. Keep, trim, or defer S8.1 items explicitly rather than deleting them wholesale. |

### 8.3 Requirements already present in design but incomplete in code

These items should not be described as missing from all repository documents:

| Direction | Existing design/plan | Current implementation evidence | Reconciliation treatment |
|---|---|---|---|
| Complete approval API | PRD Core Capabilities, architecture §14, and plan S4.2 already require list/detail/approve/reject. | `src/tether/api/routers/approvals.py` currently exposes only the decision `POST`; `CLAUDE.md` lists approval listing as a gap. | Keep the requirement, update current-status wording, and retain/refresh the implementation step. |
| Lease heartbeats | Architecture §11 explicitly says handlers heartbeat; plan S2.1 includes `worker/heartbeat.py` and heartbeat tests. | `src/tether/queue/postgres.py` explicitly says `Not yet: lease heartbeats`; `CLAUDE.md` records the same gap. | Keep the accepted design, make completion a V1 priority, and add end-to-end slow-tool proof. A new product ADR is not required unless semantics change. |
| Stable policy interface | PRD Core Capabilities, architecture §6, and ADR-0009 already define a built-in deny-by-default `PolicyEngine` with Cedar/OPA later. | `src/tether/policy/engine.py` and bootstrap policy configuration exist. | Preserve the architecture; refine trusted inputs/tool metadata and coverage rather than replacing it. |
| Governance-aware tool metadata | Architecture §7 already defines effect, risk, permissions, idempotency, preconditions, retries, timeout, and exhaustion behavior. | `src/tether/sdk/tools.py` and registry tests implement part of the contract. | Perform a gap analysis for verification/read-back metadata and avoid a parallel schema. |
| Provider-neutral model boundary | ADR-0016 and architecture §8 define neutral history and `ModelProvider`. | `src/tether/providers/` exists, but `CLAUDE.md` says only the scripted provider is implemented; `src/tether/testing/scripted.py` refuses production use. | Add the OpenAI adapter behind the existing boundary; verify no provider-specific leakage. |

### 8.4 Verified audit/code discrepancies at this snapshot

The following audit findings remain visible in the current code and should be entered in the reconciliation matrix:

1. **Replay/adoption hash validation:** `src/tether/runtime/graph.py` function `govern_call` adopts a persisted `POLICY_DECISION` by dedupe key before recomputing or comparing the current proposal's call hash. `tests/integration/test_run_graph.py::test_a_persisted_decision_is_adopted_not_re_decided` proves adoption but does not prove divergence fails closed; its seeded decision uses `call_hash: None`. Trace this through `authorize_call` and decide whether the invariant needs a code fix, a narrower guarantee, or a superseding decision.
2. **Skipped-call audit behavior:** `src/tether/runtime/graph.py` appends typed `skipped` tool messages for the remaining calls after a non-applied result, but that branch does not append an audit event. Reconcile this with ADR-0012, reconstruction claims, and any PRD requirement that every model-call disposition is audited.
3. **`created_at` MAC coverage:** architecture §16 and ADR-0015 describe `created_at` as part of the canonical audit-MAC record, but `src/tether/audit/chain.py::mac_record` currently omits it. `tests/integration/test_audit_chain.py` checks chaining/tamper behavior but does not establish timestamp coverage. Choose code conformance or an explicit guarantee revision.

These are evidence-backed gap statements, not authorization to patch the implementation during the documentation pass.

### 8.5 Exact document actions for this repository

- **`.claude/PRPs/prds/tether-runtime.prd.md`:** add a dated revision entry; update product thesis, V1 proof, automated initiation, external API, second workload, outcome verification, provider choice, scope and success metrics. Remove the final claim that no important ambiguity remains. Preserve the old decision trail in the Decisions Log.
- **`docs/adr/README.md`:** retain ADR-0001–0018 as accepted; add proposed successor ADR entries only after files exist. The first expected proposal is automatic verification/read-back versus ADR-0007.
- **`docs/architecture/tether-v1-architecture.md`:** update only after ADR conflicts are decided. Add the verification path, identity choice, real-provider/external-integration boundaries, database recovery posture, and guarantee traceability. Keep §11 heartbeat semantics unless an accepted replacement changes them.
- **`.claude/plans/tether-runtime.plan.md`:** re-baseline from the actual implementation state recorded in `CLAUDE.md`; promote the second workload and external API; select OpenAI first; keep approval listing and heartbeat completion; add guarantee-card, database-recovery, automated proof, and comparison work; re-triage S6 fallback, S8 key lifecycle, and S9 breadth explicitly.
- **`CLAUDE.md`:** update “What Tether is,” source precedence, current/active stage, V1 non-goals, and the one-step workflow for the reconciliation effort. Preserve its strong ADR, testing, security, and architecture-boundary rules. Replace the statement that machine-triggered runs are categorically outside V1 with the approved distinction from U-003 once decided.
- **`README.md`:** replace the early vision draft after the authoritative documents are reconciled. Remove or qualify RAG examples, cost budgets, inline human override, provider fallback, and broad eval language unless their final disposition supports them. Lead with the guarantee card and actual current status.
- **Create `docs/product/tether-v1-guarantees.md`:** bounded guarantee statements with code/test evidence and limitations.
- **Create a scope/traceability artifact:** either in the revised PRD or `docs/product/tether-v1-scope-delta.md`; do not create two competing sources.
- **Create `docs/operations/postgres-recovery.md`:** backup, restore, outage, recovery verification, recommended HA posture, RPO/RTO decision gates, and effects on jobs/leases/ledger/audit.
- **Update test strategy/CI documentation:** `tests/chaos`, `tests/authz`, and `tests/perf` are currently placeholders, while integration coverage exists. The plan must distinguish implemented tests from intended suites.

## 9. Documentation implications

### 8.1 PRD

Update the PRD so that it:

- opens with the product thesis and design principle in Section 3;
- distinguishes user/product outcomes from implementation mechanisms;
- assigns stable requirement IDs that trace back to `TDR-*` decisions;
- states V1 scale honestly and separates measured limits from targets;
- contains the `KEEP / CHANGE / ADD / DEFER / REMOVE` scope delta;
- includes acceptance criteria and evidence requirements for each guarantee;
- names the real-provider, external-API, second-workload, and approval-API proof;
- records unresolved decisions as gates rather than assumptions;
- explicitly lists non-goals; and
- removes or qualifies broad claims unsupported by current code/tests.

Do not simply append a new section while leaving contradictory requirements elsewhere in the PRD.

### 8.2 ADRs

1. Inventory every ADR and its status (`proposed`, `accepted`, `superseded`, etc.).
2. Map each accepted ADR to relevant `TDR-*` decisions and current implementation evidence.
3. Identify conflicts, especially the three reported in TDR-017.
4. For a changed decision, create a proposed superseding ADR with:
   - the old decision and link;
   - new context and evidence;
   - the new decision;
   - compatibility/migration implications;
   - security and failure-mode consequences; and
   - documents/tests that must change.
5. Mark the prior ADR superseded only according to the repository's process and only after the replacement is accepted.

Do not modify an accepted ADR's decision text in place merely to make the repository look consistent.

### 8.3 Architecture documentation

Update architecture material to show:

- the provider-neutral agent loop and adapter boundary;
- the complete consequential-action integrity chain;
- trusted identity/policy inputs and trust boundaries;
- exact-action approval binding and revalidation points;
- tool-contract metadata and sensitive-field handling;
- dispatch, idempotency, retry, cancellation, and crash-recovery state transitions;
- unknown-outcome verification and human-reconciliation paths;
- worker lease ownership and heartbeat behavior;
- PostgreSQL authority, outage behavior, backup/restore, and failover assumptions;
- audit/MAC construction and reconstruction guarantees; and
- dev/demo versus production boundaries.

Diagrams must match the written state model and implementation; do not leave old contradictory diagrams in place.

### 8.4 README

The README should become an accurate front door:

- lead with the concise thesis, not a generic framework feature list;
- link to the guarantee card and clearly label current versus planned guarantees;
- provide a minimal safe quick start with externally supplied credentials;
- demonstrate one human-started flow and one automated/simulated flow;
- explain approval, unknown outcomes, and reconciliation at a high level;
- link to architecture, operations, security, and scope/non-goals;
- state current operating limits and V1 status; and
- avoid claiming provider breadth, scale, HA, or external integrations not yet proven.

### 8.5 `CLAUDE.md`

Add or update repository guidance so future Claude Code sessions must:

- read this direction record, the current PRD, and all accepted ADRs relevant to a change;
- preserve the distinction between product direction, current implementation, and future plan;
- never silently contradict, rewrite, or bypass an accepted ADR;
- create a proposed superseding ADR when a decision changes;
- maintain requirement/guarantee/test traceability;
- keep provider-specific behavior behind the adapter boundary;
- preserve fail-closed policy, approval, dispatch, and production-authentication behavior;
- add/update tests whenever a guarantee changes;
- keep credentials and sensitive values out of the repository; and
- report conflicts and unresolved decisions instead of guessing.

Keep `CLAUDE.md` concise enough to remain operational. Link to this record rather than duplicating every decision.

### 8.6 Implementation plan and roadmap

Replace or revise the plan so work is ordered by dependencies and evidence, not feature count. Recommended workstreams are:

1. documentation inventory, traceability matrix, and ADR discrepancy resolution;
2. guarantee card and executable evidence map;
3. stable provider, policy, and governance-aware tool contracts;
4. approval operational API;
5. outcome verification and human reconciliation;
6. heartbeat-safe long-running execution;
7. production/dev separation and security hardening;
8. PostgreSQL backup/failover documentation and validation;
9. real OpenAI provider, real external API, and second workload;
10. automated reliability/performance suites; and
11. LangGraph/Temporal comparison and thesis review.

Each plan item should include affected requirements/decisions, dependencies, acceptance criteria, evidence/tests, documentation updates, and any decision gate.

### 8.7 Test, security, and operations documentation

Ensure supporting documents cover:

- a guarantee-to-test matrix;
- chaos/failure injection and safe credentialed-test instructions;
- authorization and approval race cases;
- reconciliation operations and audit evidence;
- production configuration and fail-closed identity behavior;
- PostgreSQL outage, backup, restore, and recovery validation;
- signing-key lifecycle limitations; and
- measured performance/operating envelopes.

## 10. Required reconciliation workflow for Claude Code

The documentation reconciliation should be performed as a reviewable change, not as a broad untraceable rewrite.

### Phase 1 — Inspect and inventory

1. Read this entire document.
2. Locate repository guidance (`CLAUDE.md`, `AGENTS.md`, contribution rules) and follow it.
3. Inventory the PRD, ADRs, architecture, README, plans/roadmaps, test strategy, security, and operations/runbook documents.
4. Inspect code and tests only as needed to verify claims and audit findings.
5. Report missing documents and duplicate/contradictory sources of truth.

### Phase 2 — Build a traceability and scope-delta matrix

Before editing, produce a matrix containing:

| Existing requirement/claim | Source file | Current evidence | Related TDR | Disposition | Proposed action | ADR impact | Validation |
|---|---|---|---|---|---|---|---|
| Example only | `path:line` | code/test or none | `TDR-###` | `KEEP/CHANGE/ADD/DEFER/REMOVE` | concise action | none/new/supersede | test/review needed |

Every material old PRD requirement and public README claim must receive a disposition. Include document paths and verified line references.

### Phase 3 — Surface conflicts before resolving them

For every accepted ADR conflict:

- quote or summarize the decision accurately;
- identify the conflicting new direction or implementation behavior;
- explain the operational/security consequences;
- recommend either implementation conformance or a proposed superseding ADR; and
- do not change the accepted decision silently.

If a choice changes security behavior, audit meaning, retry semantics, or a published guarantee, stop and request explicit approval for the proposed decision.

### Phase 4 — Reconcile documents

After the matrix is understood:

- update documents so current state, V1 target, post-V1 work, and unresolved choices are clearly separated;
- add proposed superseding ADRs where appropriate;
- update links and indexes;
- remove obsolete duplicate material only when its replacement is identified and history remains recoverable in version control;
- keep terminology and requirement IDs consistent; and
- leave code changes for a separately authorized implementation phase.

### Phase 5 — Verify the documentation change

Before declaring completion:

- search for stale or contradictory product claims;
- check relative links and ADR status/index entries;
- confirm every accepted decision is represented in the appropriate document;
- confirm every unresolved decision is still labeled unresolved;
- confirm no guarantee is stated as implemented without evidence;
- summarize all deletions, deferrals, and new ADR proposals; and
- provide a short list of decisions requiring maintainer approval.

### Expected deliverables

The reconciliation change should contain:

1. an inventory of governing documents;
2. the traceability/scope-delta matrix;
3. a revised PRD or a clearly proposed replacement;
4. ADR status corrections and proposed superseding ADRs where needed;
5. updated architecture and README;
6. concise updated `CLAUDE.md` guidance;
7. a revised, dependency-ordered implementation plan;
8. a draft guarantee card mapped to evidence; and
9. a reconciliation summary listing changed, deferred, removed, and unresolved items.

## 11. Suggested Claude Code prompt

Start a fresh Claude Code session at the repository root and provide this prompt:

```text
Perform a documentation reconciliation for Tether V1.

First read docs/product/tether-v1-direction-2026-11.md in full, then read the
repository guidance, current PRD, every accepted ADR relevant to V1, architecture
docs, README, CLAUDE.md, plans, test strategy, and operations/security docs.

Treat the direction record as the latest product-direction input, but do not
silently override an accepted ADR and do not assume the current implementation
already satisfies it. Verify the audit findings against the current code and tests.

Before editing, show me:
1. the governing-document inventory;
2. a KEEP/CHANGE/ADD/DEFER/REMOVE traceability matrix with exact file references;
3. every conflict with an accepted ADR;
4. recommendations for resolving each conflict; and
5. the unresolved decisions that block specific edits.

Then reconcile the PRD, ADR set, architecture docs, README, CLAUDE.md, guarantee
card, and implementation plan. Preserve ADR history by proposing superseding ADRs
instead of rewriting accepted decisions in place. Keep current behavior, V1 target,
post-V1 work, and unresolved decisions clearly separated. Do not make implementation
changes in this pass unless I explicitly approve them.

After editing, validate links and terminology, search for stale contradictory claims,
and give me a concise summary of changed, deferred, removed, proposed, and still-
unresolved items.
```

## 12. Maintainer acceptance checklist

The documentation reconciliation is complete only when maintainers can answer yes to all applicable items:

- [ ] The PRD and README describe the same product thesis.
- [ ] Current capabilities are distinguishable from V1 requirements and post-V1 ideas.
- [ ] Every material previous requirement has a recorded scope disposition.
- [ ] Accepted ADRs were preserved or explicitly superseded through the ADR process.
- [ ] The three reported ADR/code discrepancies have verified evidence and a resolution path.
- [ ] The guarantee card contains only bounded, testable statements.
- [ ] Each guarantee maps to implementation evidence and repeatable tests, or is labeled planned.
- [ ] Model-provider portability is architectural, and OpenAI is the first real proof rather than a core dependency leak.
- [ ] Approval list/detail/action, automatic verification, and human reconciliation are represented consistently.
- [ ] Long-running calls and lease heartbeats have explicit semantics and test plans.
- [ ] Human-started and automated scenarios are supported without overstating machine-identity maturity.
- [ ] The second workload and real external API have decision gates or accepted selections.
- [ ] Production/dev separation and fail-closed authentication expectations are explicit.
- [ ] PostgreSQL backup, restore, outage, failover, and recovery assumptions are documented.
- [ ] Scale claims are backed by measured conditions and known limits.
- [ ] Deferred and removed work is visible and justified.
- [ ] No unresolved product choice was silently decided by a documentation agent.

## 13. Record of source decisions

This direction consolidates the product decisions accepted on 2026-11-09 after reviewing Claude's audit. The accepted responses established that Tether will:

- keep owning the agent loop;
- remain model-agnostic and prove the abstraction with a real model;
- use OpenAI first with externally supplied credentials;
- create a guarantee card;
- compare Tether with LangGraph/Temporal plus custom glue;
- allow justified revision or supersession of prior requirements;
- provide a complete approval API while treating a UI as optional;
- attempt automatic real-world verification before human reconciliation;
- use a simple built-in policy engine behind a stable interface;
- target correctness at modest scale;
- add lease heartbeats;
- shrink scope that does not support the thesis;
- prove generality with unrelated workloads;
- integrate at least one external API;
- support both human-started and automated scenarios;
- defer full key-lifecycle operations;
- define a PostgreSQL backup/failover story; and
- exclude dev-only mechanisms from production operation.

This document should be updated through explicit review when one of those decisions changes. A change to the product direction should also identify the affected PRD requirements, ADRs, architecture sections, claims, tests, and plan items.
