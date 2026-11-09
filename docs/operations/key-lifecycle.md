# Signing-key lifecycle: what exists, what is manual, what is post-V1

> **Status:** V1 documentation of a stated limitation (direction record TDR-022)
> **Baseline:** commit `ec35254`

Tether uses four key families. Key IDs exist for all of them, so rotation by overlap is possible today, but **retirement checks and lifecycle automation are post-V1**. Do not describe rotation or retirement as seamless.

## 1. Key families

| Family | Where it lives | Purpose | Key ID mechanism |
|---|---|---|---|
| Audit HMAC keys | `TETHER_AUDIT_KEY_<ID>` env; active ID in `TETHER_AUDIT_ACTIVE_KEY_ID` | Per-run audit chain MAC (ADR-0015) | Each `audit_events` row stores `key_id` |
| Call-binding HMAC keys | `TETHER_BINDING_KEY_<ID>`; active ID in `TETHER_BINDING_ACTIVE_KEY_ID` | Keyed call hash (ADR-0010); independent from the audit family (PD-21) | Each run pins `binding_key_id` before its first hash |
| Workspace API keys | bootstrap YAML: `key_id`, `salt`, `secret_hash`, bound agents; raw key issued by `tether apikey new` | Calling-app authentication (ADR-0013) | `tak_<key_id>_<secret>` format (PD-20) |
| Issuer verification keys | bootstrap YAML: public PEM keys under a `kid`, pinned algorithm | JWT signature verification | Token header `kid` selects the key |

## 2. Procedures available at the baseline (manual)

### Audit key rotation
1. Add `TETHER_AUDIT_KEY_<NEW>` to the environment of `api` and `worker`; keep the old key present.
2. Set `TETHER_AUDIT_ACTIVE_KEY_ID=<NEW>`; restart. New events are MACed with the new key; chains that mix keys still verify because each event records its `key_id`.
3. **Never remove an old audit key** while any event signed with it must remain verifiable. There is no tooling that tells you when that is; in practice, audit keys are retained for the retention period of the data (undecided).

### Call-binding key rotation
1. Add `TETHER_BINDING_KEY_<NEW>`; set `TETHER_BINDING_ACTIVE_KEY_ID=<NEW>`; restart. New runs pin the new key; existing runs keep theirs.
2. Retire the old key only when no non-terminal run references it. **No command checks this yet** (`tether keys check-retirement` from plan S8.1 is post-V1). Manual check with the migrator role:
   ```sql
   SELECT count(*) FROM tether.runs
   WHERE binding_key_id = '<OLD>'
     AND status NOT IN ('succeeded','failed','cancelled','timed_out','abandoned');
   ```
   If the count is not zero, keep the key; a run whose pinned key is missing fails closed (no decision, no dispatch; ADR-0010 rule 3).

### API key rotation (overlap)
1. `tether apikey new <key_id> --agent <agent>` prints the raw key once and the bootstrap lines.
2. Add the new key to the workspace's `api_keys` in the bootstrap file; `tether bootstrap apply`; restart.
3. Move callers to the new key.
4. Remove the old key from the file; apply; restart. Keys dropped from the file are removed from the database.

### Issuer key rotation
1. Add the new public key under a new `kid` to the issuer's `public_keys`; apply; restart.
2. Have the issuer sign with the new `kid`.
3. Remove the old `kid`; apply; restart. Tokens signed with a removed `kid` are rejected (`401`).

## 3. Not available in V1 (post-V1, TDR-022)

- `tether keys check-retirement` and any automated retirement guard.
- Automated rotation schedules or reminders.
- Verification tooling for chains under retired keys beyond `tether audit verify` (itself planned for V1, D-10).
- HKDF-derived key families from a master secret (PD-21 fallback design); V1 uses independent random keys.
- Vault or cloud secret-manager integration (`SecretsProvider` seam exists).

## 4. Limitations to state in any security review

- Rotation is a manual runbook with restarts; a mistake (removing a key still referenced) makes affected runs fail closed and affected chains unverifiable, with no automated warning.
- Key material is in environment variables in V1; the compose stack reads them from a git-ignored `.env`.
- There is no key-usage inventory beyond the SQL above and the `key_id` columns.
