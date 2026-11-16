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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tether.core.canonical import canonicalize
from tether.secrets.keyring import KeyFamily, KeyRing

if TYPE_CHECKING:
    from tether.storage.repos.audit import AuditRecord, AuditRepository

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


@dataclass(frozen=True)
class ChainVerification:
    """What verifying one run's chain found. Carries no payloads, MACs or key material.

    ``reason`` is a stable code: ``sequence_mismatch`` (an event is not at the next contiguous
    ``seq``), ``prev_mac_mismatch`` (it does not link to its predecessor),
    ``unsupported_mac_version``, ``mac_mismatch`` (the recomputed MAC differs), or
    ``head_mismatch`` (the persisted head disagrees with the events). ``failed_seq`` is the stored
    ``seq`` of the first failing event, when one event is to blame.
    """

    valid: bool
    event_count: int
    last_seq: int | None
    mac_versions: tuple[int, ...]
    failed_seq: int | None = None
    reason: str | None = None
    head_seq: int | None = None
    # None when the head was not compared (the chain itself already failed).
    head_consistent: bool | None = None


def _verify_events(
    events: list[AuditRecord], keys: KeyRing, workspace_id: str, run_id: uuid.UUID
) -> ChainVerification:
    """Recompute every mac and check sequence numbers and links, stopping at the first failure."""
    versions = tuple(sorted({event.mac_version for event in events}))
    last_seq = events[-1].seq if events else None

    def failure(event: AuditRecord, reason: str) -> ChainVerification:
        return ChainVerification(
            valid=False,
            event_count=len(events),
            last_seq=last_seq,
            mac_versions=versions,
            failed_seq=event.seq,
            reason=reason,
        )

    prev_mac: str | None = None
    for expected_seq, event in enumerate(events, start=1):
        if event.seq != expected_seq:
            return failure(event, "sequence_mismatch")
        if event.prev_mac != prev_mac:
            return failure(event, "prev_mac_mismatch")
        if event.mac_version not in MAC_VERSIONS:
            return failure(event, "unsupported_mac_version")
        key = keys.key(KeyFamily.AUDIT, event.key_id).secret.reveal()
        fields: dict[str, Any] = {
            "workspace_id": workspace_id,
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
            return failure(event, "mac_mismatch")
        prev_mac = event.mac
    return ChainVerification(
        valid=True, event_count=len(events), last_seq=last_seq, mac_versions=versions
    )


async def verify_run(repo: AuditRepository, keys: KeyRing, run_id: uuid.UUID) -> bool:
    """Recompute every mac and check sequence numbers and links. False on any mismatch."""
    events = await repo.events(run_id)
    return _verify_events(events, keys, repo.scope.workspace_id, run_id).valid


async def verify_run_report(
    repo: AuditRepository, keys: KeyRing, run_id: uuid.UUID
) -> ChainVerification:
    """``verify_run`` with a diagnosis, plus a check of the persisted head (operator tooling).

    The head is read *before* the events. Appends only add events and move the head forward in
    one transaction, so a concurrent append can leave the events ahead of the head just read but
    never behind it: a consistent head names an event that exists with the same MAC. Events past
    the head are therefore not an error. A head that names a missing event, or an event whose MAC
    differs, is ``head_mismatch``. (A head that is missing while events exist is also a mismatch,
    so callers should read both in one snapshot, as the CLI does with REPEATABLE READ: otherwise
    a run's very first append could land between the two reads.) This catches deleting events
    while leaving the head alone; it cannot catch deleting them *and* rewriting the head, nor
    deleting the whole run (ADR-0015 rule 8).
    """
    head = await repo.head(run_id)
    events = await repo.events(run_id)
    result = _verify_events(events, keys, repo.scope.workspace_id, run_id)
    head_seq = None if head is None else head.seq
    if not result.valid:
        return replace(result, head_seq=head_seq)
    if not events:
        consistent = head is None or head.seq == 0
    else:
        consistent = (
            head is not None
            and 0 < head.seq <= len(events)
            and events[head.seq - 1].mac == head.last_mac
        )
    return replace(
        result,
        valid=consistent,
        reason=None if consistent else "head_mismatch",
        head_seq=head_seq,
        head_consistent=consistent,
    )
