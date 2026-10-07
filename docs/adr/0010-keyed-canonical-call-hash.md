# ADR-0010: Approval binding: RFC 8785 canonical envelope with keyed (HMAC) call hash

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-11; AD-09 (key input); AD-14; §24 items 11, 24 (A-05)
**Resolves**: A-05

## Context

An approval must bind the exact action. Serialisation must be stable across processes, retries, and library versions. The PRD's approval detail shows the "canonical hashed tool call" next to *masked* arguments, and audit and inspection views also show the hash.

A plain SHA-256 over the canonical envelope of *unmasked* arguments can be brute-forced by any viewer (approver, auditor, requester) when a sensitive argument has low entropy, such as a short code or an enum-like secret. The viewer simply hashes candidate values together with the displayed envelope fields. That defeats masking (T-16).

## Decision

1. **Canonical envelope (unchanged from AD-11).**
   - It is versioned and contains workspace, run, agent, tool, tool version, arguments, and preconditions.
   - Arguments are the *validated* input model in JSON mode, **unmasked**, so the hash binds the real action.
   - The model's call ID and rationale are excluded.
   - The envelope is canonicalised with **RFC 8785 JCS**.
   - Write-tool argument schemas avoid binary floats; strings are hashed as given.
2. **`call_hash` = HMAC-SHA-256 over the canonical envelope**, keyed with a **call-binding key** from `SecretsProvider`.
   - The key is distinct from the audit-chain key. Purposes stay separate; any derivation from a common secret must use a distinct label, and the scheme is a planning detail.
   - The key is never stored in the database.
   - Only worker processes need it; `api` never computes call hashes.
3. **Pinning and rotation.**
   - Before its first call hash is computed, each run is pinned (by a fenced driver write) to the active binding-key ID, and keeps that key for life.
   - Rotation activates a new key ID for new runs only.
   - Retired keys stay available in `SecretsProvider` until no non-terminal run references them.
   - If a run's pinned key is missing, hashing fails closed: no decision, no dispatch; the job retries and then goes through TX12.
4. **Display.** Approval detail, inspection, and audit show:
   - the keyed hash, as a binding fingerprint;
   - masked arguments;
   - precondition values (masked if marked `Sensitive`);
   - requester and expiry;
   - model rationale labelled untrusted.
   Showing the hash is safe because it cannot be recomputed without the key.
5. **Idempotency keys** are derived from the keyed hash (ADR-0008) and stored when the ledger row is created, so key rotation never changes an existing idempotency key.
6. **T-08 hash-binding semantics are unchanged.** Any change to tool, tool version, arguments, or preconditions still changes the hash and invalidates the approval. All equality comparisons — approval vs ledger vs decision events vs the worker's pre-dispatch check — behave exactly as before, because the HMAC is a deterministic function of the same envelope under the run's pinned key.

## Alternatives Considered

### Plain SHA-256 (the original AD-11)
- **Pros**: No key management.
- **Cons**: Low-entropy sensitive arguments can be recovered from the displayed hash.
- **Why not**: It leaks data that masking is supposed to protect.

### Hide the hash from approvers
- **Pros**: No display leak.
- **Cons**: The PRD requires showing the canonical hashed call, and the hash still appears in audit records visible to auditors.
- **Why not**: Contradicts the PRD and only moves the leak.

### Per-run random nonce in the envelope, stored on the run row
- **Pros**: No key operations or rotation.
- **Cons**: The nonce sits in the database next to the hash. Once checkpoint encryption lands (post-V1), the hash and nonce would become the weakest link for database readers, and any API that exposes the nonce breaks the protection.
- **Why not**: A `SecretsProvider` key gives equal protection now and stays sound after checkpoint encryption, and the key-ID rotation pattern already exists for the audit chain.

### Reuse the audit HMAC key
- **Pros**: One secret.
- **Cons**: One key used for two purposes; the two rotations become coupled.
- **Why not**: Purpose separation is cheap.

### Ad-hoc `json.dumps(sort_keys=True)`; hashing raw model output
- **Pros**: Trivial.
- **Cons**: Number formatting and escaping differ across libraries. Hashing raw output binds unvalidated text instead of the action that will actually execute.
- **Why not**: Unstable, and binds the wrong thing.

## Rationale

Keying the hash removes the brute-force channel without changing what is bound or how bindings are compared. It reuses the existing `SecretsProvider` and key-ID rotation pattern.

## Consequences

### Positive
- The fingerprint can be displayed safely, and masked arguments cannot be recovered from it.
- T-08 is unchanged, and targets see nothing new.

### Negative
- A key-management and rotation runbook is needed.
- Workers depend on `SecretsProvider` for every hash; an outage stops progress.
- If a live run's pinned key is lost, that run cannot proceed; it fails closed.

### Risks
- An operator retires a key while runs still use it. Mitigation: retirement is checked against non-terminal runs that reference the key ID.

## Failure and Recovery

- Key unavailable: no decision and no dispatch; jobs retry, then TX12.
- Replays use the same pinned key, so they produce identical hashes.

## Invariants and Constraints

- §22 invariants 3, 11, 14, 15.
- New invariant **34** (added to architecture §22). Call hashes are HMAC-SHA-256 under the run's pinned call-binding key, which is distinct from the audit key and never enters the database.

## Revisit When

- Approvers need independent verification (for example, signed approval payloads).
- Secrets move to Vault or an HSM.
- Unicode normalisation of string arguments becomes necessary.

## References

- PRD: Core Capabilities (approval gate bound to a canonical hash); Approval binding and preconditions; Authorization for reads and human actions (approval detail); Sensitive data; Threat model; tests T-08, T-16, T-19, T-23.
- Architecture: §14 AD-11, §12 AD-09, §17 AD-14, §25 (JCS integer range), §24 items 11, 24, §27 A-05, D-10.
