"""Per-event audit MAC version (W2.4, D-6.3, ADR-0015 rule 1, AD-12).

``mac_version`` records how an event's own MAC was computed. Every event that exists when this
revision runs was MACed without its timestamp: it is version 1. Adding the column with a constant
default is a metadata-only change in Postgres, so no existing row is rewritten. The default is
then dropped, so every later write must state its version explicitly (the application writes 2);
a writer that forgets cannot silently produce a version-1 event.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    "ALTER TABLE tether.audit_events ADD COLUMN mac_version smallint NOT NULL DEFAULT 1",
    "ALTER TABLE tether.audit_events ALTER COLUMN mac_version DROP DEFAULT",
    # Positive only; which versions exist is the verifier's business, and it fails closed on
    # any it does not know.
    "ALTER TABLE tether.audit_events"
    " ADD CONSTRAINT ck_audit_events_mac_version CHECK (mac_version >= 1)",
]

DOWNGRADE = [
    "ALTER TABLE tether.audit_events DROP CONSTRAINT ck_audit_events_mac_version",
    "ALTER TABLE tether.audit_events DROP COLUMN mac_version",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
