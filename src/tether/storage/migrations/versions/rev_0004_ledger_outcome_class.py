"""Ledger outcome classification (W3.2, ADR-0021 rule 10, owner decision U-008 option A).

``outcome_class`` records Tether's own classification of an execution: ``applied``,
``not_applied`` (definitive evidence the action did not apply), ``unknown`` (the external outcome
cannot be determined safely) or ``failed`` (a definitive ordinary failure that asserts nothing
about a consequential action). The column is nullable and has no default: entries recorded
before this revision stay unclassified and are never back-filled, and every new outcome write
states its classification explicitly. Adding a nullable column without a default rewrites no
row. The existing table-level grants already cover the new column.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    "ALTER TABLE tether.ledger_entries ADD COLUMN outcome_class text",
    "ALTER TABLE tether.ledger_entries ADD CONSTRAINT ck_ledger_entries_outcome_class"
    " CHECK (outcome_class IS NULL"
    " OR outcome_class IN ('applied', 'not_applied', 'unknown', 'failed'))",
]

DOWNGRADE = [
    "ALTER TABLE tether.ledger_entries DROP CONSTRAINT ck_ledger_entries_outcome_class",
    "ALTER TABLE tether.ledger_entries DROP COLUMN outcome_class",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
