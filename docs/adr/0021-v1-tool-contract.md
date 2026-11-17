# ADR-0021: The V1 governed tool contract: preconditions, timeout, exhaustion, outcome classification, `ToolSource` and pinned versions

**Date**: 2026-11-15
**Status**: accepted (2026-11-15; proposed and accepted under owner decision U-008, tool-contract half, W3.2)
**Deciders**: Tether project owner (SwapnilJadhav11)
**Architecture source**: §7, §12.3, §14; invariants 5, 33; PRD "Approval binding and preconditions", "Dispatch and side-effect outcome semantics"; product direction TDR-009; owner decisions OD-6, U-008 (2026-11-15); PD-17, PD-28

## Context

TDR-009 requires the tool contract to carry governance semantics end to end. W3.1 (ADR-0020) added `risk` and `required_permissions`. Architecture §7 lists the remaining parts (preconditions, timeout, `on_exhausted`, the `ToolSource` protocol, the ADR-0019 `verify` declaration) without an exact schema, and leaves the schema to U-008. The baseline also had three behaviours that this contract has to fix:

- the call envelope already had a `preconditions` field and approvals a `preconditions_masked` column, both always empty;
- TX5 and every replay hash check resolved the *currently offered* tool version, so a version bump during a run surfaced as "call changed" or as `REPLAY_DIVERGENCE`, contrary to ADR-0011 rules 1–3;
- a definitive rejection by the tool (`ToolNotApplied`) reached the model as `failed`, indistinguishable from an exhausted read.

The owner decided the exact contract on 2026-11-15. Accepted ADRs are not edited; this ADR records the contract and the clarifications of ADR-0011, ADR-0012 and ADR-0019 it relies on.

## Decision

### Descriptor

1. **`ToolDescriptor` fields.** Required: `name`, `version`, `description`, `input_model`, `output_model`, `effect`, `handler`, `risk`, `required_permissions`. Required for writes, absent for reads: `idempotency`. Defaulted: `max_attempts = 3` (PD-14), `timeout_seconds = 10.0` (PD-17), `on_exhausted = "typed_failure"`. Optional: `preconditions = ()`, `verify = None`. Registration rejects every combination this ADR does not allow.

### Preconditions (target-enforced)

2. **A precondition is declarative.** `preconditions` is a tuple of top-level input field names, allowed on write tools only. Each named field must exist in the input model, be required and have no default. There is no precondition callable in V1, and Tether never reads the external world to check one.
3. **Three kinds of value are kept apart.** The *declaration* (the field names) is fixed by the tool version. The *expected values* are model-supplied arguments, validated by the input model. The *authoritative current value* lives only in the target; Tether never reads, stores or hashes it, so replay stays deterministic.
4. **The target enforces the precondition atomically with the write.** On a mismatch it must apply nothing; the handler then raises `ToolNotApplied`. Only such a definitive rejection is `not_applied`. A timeout, reset or ambiguous error while the target checks is `unknown`. A client-side read-then-write inside a handler is not a precondition.
5. **Binding.** The declared names and their expected values fill the existing envelope `preconditions` field (envelope version unchanged, PD-28). The approval row and the `APPROVAL_REQUESTED` / `EXECUTION_AUTHORIZED` audit payloads carry them masked (`Sensitive` fields replaced, ADR-0017); the hash and the handler use the real values.

### Timeout and exhaustion

6. **Timeout.** `timeout_seconds` bounds one handler attempt (one send). It must be finite, greater than 0 and at most 300. It is independent of the run deadline: an in-flight call is never clipped to the remaining deadline. A timeout is always `unknown`, never `not_applied`; the existing rules then apply unchanged (a read is retried until its budget is spent; an idempotent write is re-sent with the same key through the stop-gated TX6r record; a non-idempotent write is never re-sent). Until lease heartbeats exist (W6.1), worker startup refuses any tool, offered or retained, whose `timeout_seconds` is not below `worker_lease_seconds`.
7. **`on_exhausted`** is `typed_failure` (default) or `fail_run`. `fail_run` is allowed on read tools only and triggers only when the read's final outcome classification is `unknown`: its send budget was spent, or TX12 resolved it after dispatch. A `not_applied` or `failed` read never triggers it.
8. **`fail_run` is a terminating result under ADR-0012 rule 6.** When it triggers, no further model call happens, and the remaining proposed calls of the turn are neither governed nor marked skipped: **no `CALL_SKIPPED` events are emitted for them**, and no history is appended. With `typed_failure`, the model receives the typed `failed` result and ADR-0012 rule 4 applies as usual.
9. **`fail_run` never hides external truth.** While any ledger entry of the run is unresolved (`pending`, `dispatched` or `indeterminate`), the run is held in `awaiting_reconciliation` (ADR-0018) and is not terminalized. Once every entry is resolved, the established lifecycle rules decide: if the stop predicate is true the run ends `cancelled` or `timed_out` (ADR-0006 stop precedence) with `tool_exhausted:<tool>` named in the summary; otherwise it ends `failed` with the summary `tool_exhausted:<tool>`.

### Outcome classification

10. **`ledger_entries.outcome_class`** (migration `0004`) records Tether's own classification of an execution: `applied` (definitive successful completion), `not_applied` (definitive evidence that the requested action did not apply: the tool's `ToolNotApplied`, or a refusal before anything was sent), `unknown` (Tether cannot safely determine the external outcome), `failed` (a definitive ordinary failure that is neither applied nor a positive assertion that a consequential action did not occur, such as a read whose output violates its schema). A database CHECK enforces the closed set. The column is nullable: rows recorded before `0004` stay unclassified and are never back-filled. Every new runtime outcome write states the classification. Reconciliation (TX8) does not change it; `resolution` records who resolved the entry.
11. **Model-facing results.** A `failed` entry classified `not_applied` reaches the model as `not_applied`; one classified `unknown` or `failed` reaches it as `failed`; unclassified historical rows keep the baseline mapping (`failed`). Reconciled resolutions keep their existing mapping. Writes with an unknown outcome stay `indeterminate` (ADR-0019 rule 4).

### `ToolSource`, retained versions and pinned resolution

12. **`ToolSource`** is a stable, workload-scoped protocol: `name`, `list_tools()` (the offered tools, one version per name) and `resolve(name, version)` (an exact pinned descriptor, offered or retained, or nothing). Its methods are pure and synchronous and return the same answers for the life of the process. It is neither workspace- nor agent-aware; `ToolRegistry` keeps enforcing the agent's offered tools and renders the manifest. `ToolDescriptor` remains the executable type (no separate `ExecutableTool`). The V1 implementation is `NativePythonToolSource(workload)`. This clarifies the §7 sketch `list_tools(workspace)` / `ExecutableTool`.
13. **Retained versions (ADR-0011 rule 2).** `Workload.retained_tools` lists earlier versions that stay resolvable but are never offered to a new model turn. A `(name, version)` pair is unique across offered and retained tools.
14. **Pinned resolution (ADR-0011 rules 1, 3, 4).** TX2 pins the offered version and records it in the decision event. Every later lookup for that call (replay adoption, approval request and continuation, TX5, the ledger wait, and worker execution) resolves exactly the pinned `(name, version)`. Nothing ever falls forward to another version. At TX5, a resolvable pinned descriptor is the one evaluated; its `risk`, `required_permissions` and other metadata are bound to that version. Security changes take effect through the **current policy** (role and agent permissions, rules, policy version), which is the revocation mechanism for an already-pinned action.
15. **Pinned version gone before a ledger row exists:** the call gets a typed `not_applied` result ("tool version unavailable"), nothing is sent, and it is not reported as `REPLAY_DIVERGENCE`. The decision-class audit event ADR-0011 rule 3 asks for on that path is not written yet; it belongs with the other TX5 refusal events (guarantee P-16). **Pinned version gone once a ledger row exists** is unchanged here and deferred to W5.2 (ADR-0011 rule 5, version-gone-after-dispatch); it is never hidden (ADR-0018).

### Call hash

16. The call hash binds the tool name, the pinned version, the arguments and the declared precondition names and values. `timeout_seconds`, `on_exhausted`, `verify`, `risk`, `required_permissions`, `max_attempts`, `effect` and `idempotency` are not hashed separately: they are bound by the pinned version (ADR-0011 version-bump rule). Tools without preconditions keep byte-identical hashes.

### Verification declaration (ADR-0019, declaration only)

17. A write tool may declare `verify: VerifyDeclaration` with `probe` (an async read-back taking the original validated arguments and a `VerifyContext` holding the idempotency key, returning a `VerificationResult` of kind `confirmed_applied`, `confirmed_not_applied` or `inconclusive` with evidence), `evidence_model` (`Sensitive`-annotated, for ADR-0017 masking), `required_permissions`, `max_attempts = 3` and `backoff` (base 1 s, ×2, cap 30 s, full jitter). This is a naming clarification of ADR-0019's `verify`, `verify_max_attempts` and `verify_backoff`; their meaning is unchanged. The probe uses the tool's `timeout_seconds`. **W3.2 only validates and stores the declaration; nothing invokes a probe** until W5.1.
18. **Open for W5.1 (blocker, not solved here).** ADR-0019 rule 8 evaluates current policy for the verify operation and treats `require_approval` as `deny`. Under the built-in policy (one rule per tool, ADR-0020), an approval-gated write's rule is `require_approval`, so its verification cannot be authorized as the policy stands. How the verify operation maps to policy is a W5.1 design decision; verification authorization is not claimed.

## Alternatives Considered

### Tether-evaluated precondition callables
- **Pros**: Works with targets that lack compare-and-swap.
- **Cons**: A separate read before the write has a time-of-check gap; Tether would need world-state reads; mutable values would enter replay.
- **Why not**: Only an atomic check in the target makes a stale change impossible.

### Classification encoded in `outcome_reason` prefixes
- **Pros**: No migration.
- **Cons**: The honest-outcome boundary would depend on parsing free text that also carries tool-authored reasons.
- **Why not**: Owner decision 2 (U-008): a typed column with a CHECK.

### `fail_run` emitting skip events for the remaining calls
- **Pros**: Every proposed call would have its own decision-class event.
- **Cons**: Contradicts ADR-0012 rule 6, which lists a read tool configured to fail the run among terminating results.
- **Why not**: Owner decision 4: terminating results do not skip.

### Re-evaluating TX5 with the newly offered descriptor
- **Pros**: A risk or permission change in a new version would apply to in-flight calls.
- **Cons**: The executed code would differ from the governed and approved code (ADR-0011 rule 1).
- **Why not**: Current policy is the revocation mechanism; descriptor metadata is version-bound.

### Hashing every descriptor field
- **Pros**: Catches a descriptor change shipped without a version bump.
- **Cons**: Turns a policy-metadata change into a hash mismatch instead of a fresh policy decision; cannot cover handler code anyway.
- **Why not**: The version is the binding; the version-bump rule (ADR-0011 rule 2) is the author's obligation.

## Consequences

### Positive
- Stale-world protection (T-21) is expressible and bound into approvals; exhausted reads can end the run (T-04) without hiding effects.
- Deployments that bump a tool version no longer read as replay divergence before a ledger row exists, and retained versions let in-flight calls drain.
- Classification is explicit and queryable.

### Negative
- Migration `0004`. It is additive and nullable, so an older writer running against it only leaves new rows unclassified; it is still rolled out like `0003` (stop the old `api` and `worker`, migrate, start the new ones), and a downgrade below `0004` discards classifications.
- Tool authors must forward precondition values correctly and keep a target-side atomic check; Tether cannot detect a handler that ignores them.
- Until W6.1, a tool cannot declare a timeout at or above the worker lease.

### Risks
- A descriptor changed without a version bump executes under the old approval. Mitigation: the version-bump rule, review, and the identical-`(name, version)` uniqueness check inside a workload.

## Failure and Recovery

- Crash after TX7 of an exhausted read with `fail_run`: replay re-reads the ledger and reaches the same terminating path; the terminal status is written once (TX11).
- Crash after a precondition rejection: the ledger outcome is adopted; nothing is re-sent.
- Pinned version gone after a ledger row exists: unchanged from the baseline until W5.2.

## Invariants and Constraints

- §22 invariants 1, 4, 5, 9, 22, 33 and 35 unchanged. Every new outcome write states `outcome_class`. `fail_run` never terminalizes a run over an unresolved entry. No lookup after TX2 falls forward to an unpinned version.

## Revisit When

- Lease heartbeats exist (W6.1): the startup bound on `timeout_seconds` can relax.
- W5.1 decides the verify-operation policy mapping (rule 18).
- W5.2 implements the post-ledger version-gone path and PD-33.
- MCP or remote tool sources are added: they implement the same `ToolSource` protocol.

## References

- PRD (revision 2026-11-09): Approval binding and preconditions; Dispatch and side-effect outcome semantics; tests T-04, T-08, T-21.
- Architecture: §7, §12.3, §14, §22.
- ADRs: [ADR-0006](0006-run-lifecycle-single-status-writer.md), [ADR-0008](0008-idempotency-keys-and-stop-gated-resends.md), [ADR-0010](0010-keyed-canonical-call-hash.md), [ADR-0011](0011-tool-version-pinning-approval-validity.md), [ADR-0012](0012-sequential-multi-call-governance.md), [ADR-0017](0017-sensitive-data-masking-choke-point.md), [ADR-0018](0018-awaiting-reconciliation-hold.md), [ADR-0019](0019-automatic-verification-of-unknown-outcomes.md), [ADR-0020](0020-v1-policy-engine-contract.md).
- Product: direction record TDR-009; scope delta P-2, P-8, U-008.
