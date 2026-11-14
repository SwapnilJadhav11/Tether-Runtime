"""Per-run HMAC audit chain (ADR-0015).

``mac = HMAC-SHA-256(K_audit, JCS(record))`` where the record includes the previous event's mac,
so each run's events form a chain. The key ID is pinned per run on its ``audit_heads`` row.

Each event records how its own MAC is computed (``mac_version``, W2.4); one run's chain may mix
versions, and every link is still the previous event's stored MAC:

- version 1, every event written before migration ``0003``: ``mac_record``, which does not cover
  the event's time. Such events stay valid as written; their timestamps were never protected.
- version 2, every new event: ``mac_record_v2``, the same fields plus the version and
  ``created_at`` (ADR-0015 rule 1 "time"; AD-12).

Any other version fails verification closed.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tether.core.canonical import canonicalize
from tether.secrets.keyring import KeyFamily, KeyRing

if TYPE_CHECKING:
    from tether.storage.repos.audit import AuditRepository

# The version every new event is written with.
MAC_VERSION = 2
# The versions this code can verify; any other is a failed verification, never a pass.
MAC_VERSIONS = frozenset({1, 2})


def compute_mac(key: bytes, record: dict[str, Any]) -> str:
    return hmac.new(key, canonicalize(record, allow_floats=True), hashlib.sha256).hexdigest()


def canonical_created_at(value: datetime) -> str:
    """The one text form of an event time inside a v2 MAC record.

    UTC, ISO 8601, exactly six fractional digits (Postgres ``timestamptz`` stores microseconds),
    suffix ``Z``. Numeric fields only, so the result does not depend on locale, and the same
    instant gives the same text whatever offset it was read in.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an audit event time must carry a timezone")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    """The version-1 record. Unchanged since the chain was introduced: do not edit."""
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


def mac_record_v2(
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
    created_at: datetime,
) -> dict[str, Any]:
    """The version-2 record: version 1 plus the version itself and the stored ``created_at``.

    The version is inside the MAC, so changing a stored version (for example 2 → 1, which would
    drop the time from the check) invalidates the event.
    """
    record = mac_record(
        workspace_id=workspace_id,
        run_id=run_id,
        seq=seq,
        dedupe_key=dedupe_key,
        event_type=event_type,
        actor=actor,
        tool=tool,
        payload=payload,
        policy_version=policy_version,
        prev_mac=prev_mac,
    )
    return {**record, "mac_version": 2, "created_at": canonical_created_at(created_at)}


async def verify_run(repo: AuditRepository, keys: KeyRing, run_id: uuid.UUID) -> bool:
    """Recompute every mac and check sequence numbers and links. False on any mismatch."""
    prev_mac: str | None = None
    for expected_seq, event in enumerate(await repo.events(run_id), start=1):
        if event.seq != expected_seq or event.prev_mac != prev_mac:
            return False
        if event.mac_version not in MAC_VERSIONS:
            return False
        key = keys.key(KeyFamily.AUDIT, event.key_id).secret.reveal()
        fields: dict[str, Any] = {
            "workspace_id": repo.scope.workspace_id,
            "run_id": run_id,
            "seq": event.seq,
            "dedupe_key": event.dedupe_key,
            "event_type": event.event_type,
            "actor": event.actor,
            "tool": event.tool,
            "payload": event.payload,
            "policy_version": event.policy_version,
            "prev_mac": event.prev_mac,
        }
        record = (
            mac_record(**fields)
            if event.mac_version == 1
            else mac_record_v2(**fields, created_at=event.created_at)
        )
        if not hmac.compare_digest(compute_mac(key, record), event.mac):
            return False
        prev_mac = event.mac
    return True
