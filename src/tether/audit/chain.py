"""Per-run HMAC audit chain (ADR-0015).

``mac = HMAC-SHA-256(K_audit, JCS(record))`` where the record includes the previous event's mac,
so each run's events form a chain. The key ID is pinned per run on its ``audit_heads`` row.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import TYPE_CHECKING, Any

from tether.core.canonical import canonicalize
from tether.secrets.keyring import KeyFamily, KeyRing

if TYPE_CHECKING:
    from tether.storage.repos.audit import AuditRepository


def compute_mac(key: bytes, record: dict[str, Any]) -> str:
    return hmac.new(key, canonicalize(record, allow_floats=True), hashlib.sha256).hexdigest()


def mac_record(
    *,
    workspace_id: str,
    run_id: uuid.UUID,
    seq: int,
    dedupe_key: str,
    event_type: str,
    actor: str | None,
    tool: str | None,
    payload: dict[str, Any],
    policy_version: str | None,
    prev_mac: str | None,
) -> dict[str, Any]:
    return {
        "workspace_id": workspace_id,
        "run_id": str(run_id),
        "seq": seq,
        "dedupe_key": dedupe_key,
        "event_type": event_type,
        "actor": actor,
        "tool": tool,
        "payload": payload,
        "policy_version": policy_version,
        "prev_mac": prev_mac,
    }


async def verify_run(repo: AuditRepository, keys: KeyRing, run_id: uuid.UUID) -> bool:
    """Recompute every mac and check sequence numbers and links. False on any mismatch."""
    prev_mac: str | None = None
    for expected_seq, event in enumerate(await repo.events(run_id), start=1):
        if event.seq != expected_seq or event.prev_mac != prev_mac:
            return False
        key = keys.key(KeyFamily.AUDIT, event.key_id).secret.reveal()
        record = mac_record(
            workspace_id=repo.scope.workspace_id,
            run_id=run_id,
            seq=event.seq,
            dedupe_key=event.dedupe_key,
            event_type=event.event_type,
            actor=event.actor,
            tool=event.tool,
            payload=event.payload,
            policy_version=event.policy_version,
            prev_mac=event.prev_mac,
        )
        if not hmac.compare_digest(compute_mac(key, record), event.mac):
            return False
        prev_mac = event.mac
    return True
