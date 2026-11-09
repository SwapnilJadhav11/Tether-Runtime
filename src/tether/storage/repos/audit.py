"""Audit events, appended inside the caller's transaction (ADR-0015).

``append`` is get-or-create by ``(run_id, dedupe_key)``: a decision event that already exists is
returned unchanged, so a replayed node adopts the persisted decision instead of re-deciding.
The run's ``audit_heads`` row is locked ``FOR UPDATE`` to serialize appends per run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping

from tether.audit.chain import compute_mac, mac_record
from tether.observability import tracing
from tether.secrets.keyring import KeyFamily, KeyRing
from tether.storage import tables
from tether.storage.repos.base import ScopedRepository

_HEADS = tables.audit_heads


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    dedupe_key: str
    event_type: str
    actor: str | None
    tool: str | None
    payload: dict[str, Any]
    policy_version: str | None
    prev_mac: str | None
    mac: str
    key_id: str
    created: bool = False

    @classmethod
    def from_row(cls, row: RowMapping) -> AuditRecord:
        return cls(
            seq=row["seq"],
            dedupe_key=row["dedupe_key"],
            event_type=row["event_type"],
            actor=row["actor"],
            tool=row["tool"],
            payload=row["payload_masked"],
            policy_version=row["policy_version"],
            prev_mac=row["prev_mac"],
            mac=row["mac"],
            key_id=row["key_id"],
        )


class AuditRepository(ScopedRepository):
    table = tables.audit_events

    async def find(self, run_id: uuid.UUID, dedupe_key: str) -> AuditRecord | None:
        statement = self.scoped(
            select(self.table).where(
                self.table.c.run_id == run_id, self.table.c.dedupe_key == dedupe_key
            )
        )
        row = (await self._connection.execute(statement)).mappings().first()
        return None if row is None else AuditRecord.from_row(row)

    async def events(self, run_id: uuid.UUID) -> list[AuditRecord]:
        statement = self.scoped(
            select(self.table).where(self.table.c.run_id == run_id).order_by(self.table.c.seq)
        )
        rows = (await self._connection.execute(statement)).mappings().all()
        return [AuditRecord.from_row(row) for row in rows]

    async def append(
        self,
        keys: KeyRing,
        *,
        run_id: uuid.UUID,
        dedupe_key: str,
        event_type: str,
        payload: dict[str, Any],
        actor: str | None = None,
        tool: str | None = None,
        policy_version: str | None = None,
    ) -> AuditRecord:
        """Payloads must already be masked (ADR-0017)."""
        workspace_id = self.scope.workspace_id
        await self._connection.execute(
            pg_insert(_HEADS)
            .values(
                run_id=run_id,
                workspace_id=workspace_id,
                key_id=keys.active_key_id(KeyFamily.AUDIT),
            )
            .on_conflict_do_nothing(index_elements=[_HEADS.c.run_id])
        )
        locked = await self._connection.execute(
            select(_HEADS)
            .where(_HEADS.c.run_id == run_id, _HEADS.c.workspace_id == workspace_id)
            .with_for_update()
        )
        head = locked.mappings().one()
        existing = await self.find(run_id, dedupe_key)
        if existing is not None:
            return existing
        record = AuditRecord(
            seq=head["seq"] + 1,
            dedupe_key=dedupe_key,
            event_type=event_type,
            actor=actor,
            tool=tool,
            payload=payload,
            policy_version=policy_version,
            prev_mac=head["last_mac"],
            mac="",
            key_id=head["key_id"],
            created=True,
        )
        key = keys.key(KeyFamily.AUDIT, record.key_id).secret.reveal()
        mac = compute_mac(
            key,
            mac_record(
                workspace_id=workspace_id,
                run_id=run_id,
                seq=record.seq,
                dedupe_key=dedupe_key,
                event_type=event_type,
                actor=actor,
                tool=tool,
                payload=payload,
                policy_version=policy_version,
                prev_mac=record.prev_mac,
            ),
        )
        await self._connection.execute(
            insert(self.table).values(
                workspace_id=workspace_id,
                run_id=run_id,
                seq=record.seq,
                dedupe_key=dedupe_key,
                event_type=event_type,
                actor=actor,
                tool=tool,
                payload_masked=payload,
                policy_version=policy_version,
                # Correlation only, outside the MAC (ADR-0015 §7, architecture §16).
                trace_id=tracing.current_trace_id(),
                prev_mac=record.prev_mac,
                mac=mac,
                key_id=record.key_id,
            )
        )
        await self._connection.execute(
            update(_HEADS).where(_HEADS.c.run_id == run_id).values(seq=record.seq, last_mac=mac)
        )
        return AuditRecord(**{**record.__dict__, "mac": mac})
