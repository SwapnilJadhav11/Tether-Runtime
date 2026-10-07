# ADR-0017: Sensitive-data masking at a single choke point

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-14; §26; §24 item 14; review finding D-07

## Context

The PRD requires that `SecretsProvider` secrets and schema-declared sensitive fields never reach:
- model context;
- inspection responses (including data derived from checkpoints);
- logs or traces;
- audit;
- approval displays.

Sinks multiply over time, so redacting at each sink eventually misses one.

## Decision

1. **Masking at the source.** Tool schemas mark sensitive fields through the SDK. The worker's result sanitizer masks them **before the outcome transaction**. Unmasked sensitive output is never persisted, checkpointed, logged, traced, audited, displayed, or sent to a model.
2. **Arguments.** Arguments are hashed unmasked for binding (keyed hash, ADR-0010). They are stored and displayed masked in audit, approvals, logs, and inspection. Unmasked arguments exist only in the model-produced history (the checkpoint) and in the job payload needed to execute the call.
3. **Secrets.** Secrets come through `SecretsProvider` with a masked representation, and known secret values are registered with the log and trace scrubber.
4. **Leak paths closed by configuration.**
   - Validation errors never echo their inputs.
   - Third-party framework tracing is disabled.
   - Graph state and job payloads are never logged.
5. **Uniform masking** for every viewer. V1 has no field-level views.

## Alternatives Considered

### Redact at each sink
- **Pros**: Each sink controls its own output.
- **Cons**: Every new sink is a new leak risk.
- **Why not**: One tested boundary is safer.

### Redact only known secrets
- **Pros**: Simpler.
- **Cons**: Tool outputs can contain secrets the platform doesn't know about.
- **Why not**: The PRD requires schema-declared sensitive fields.

### Persist unmasked output and mask on read
- **Pros**: Supports richer views later.
- **Cons**: Plaintext sits at rest, and every reader must remember to mask.
- **Why not**: The opposite of masking at the source.

### Encrypt checkpoints now
- **Pros**: Protects unmasked arguments at rest.
- **Cons**: Extra scope in V1.
- **Why not**: Explicitly post-V1 in the PRD.

## Rationale

One choke point is one thing to test (T-16), and new sinks are safe by default.

## Consequences

### Positive
- T-16 is provable, and new sinks inherit masking.

### Negative
- A mislabelled field leaks.
- The model cannot reason over sensitive values (by design).
- Unmasked arguments sit in checkpoints and job payloads until encryption at rest exists.
- Non-sensitive output still leaves the deployment when a hosted provider is used (a documented egress).

### Risks
- Missing `Sensitive` annotations. Mitigation: review of tool schemas, plus T-16.

## Invariants and Constraints

- §22 invariants 11, 14.

## Revisit When

- Checkpoint encryption at rest is added.
- Role-specific field views are required.
- The data-retention policy is set.
- Untrusted tools are supported.

## References

- PRD: Sensitive data; Core Capabilities (`SecretsProvider`, sensitive fields); Decisions Log (Sensitive data, Secrets); test T-16.
- Architecture: §17 AD-14, §26, §24 item 14, §27 D-07.
