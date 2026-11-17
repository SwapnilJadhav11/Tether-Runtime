"""Automatic verification of unknown outcomes (W5.1, ADR-0019 rules 9-12, ADR-0022).

- ``ledger_entries.verification_attempts``: probes started for the entry, counted by the
  verification gate (TX-V1) before each probe. ``NOT NULL DEFAULT 0`` with ``CHECK (>= 0)``.
  A constant default is stored in the catalogue, so existing rows are not rewritten; they read 0.
- ``ledger_entries.verification_evidence_masked``: the masked evidence of the definitive probe
  result that resolved the entry (TX-V2). Nullable, no default.
- ``resolution`` gains ``verified_applied`` and ``verified_not_applied``. A verified resolution
  needs its resolver (``system:verification``) and its evidence, as a reconciled one needs its
  reconciler and justification.
- ``jobs.kind`` gains ``verify_outcome``.

The existing table-level grants already cover the new columns; the app role still has no DELETE.

Downgrade restores the ``0004`` constraints. It is refused by those constraints once a
``verified_*`` entry or a ``verify_outcome`` job exists: a downgrade would otherwise hide who
resolved an entry. It is not a production rollback (as ``0003``).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESOLUTIONS_0004 = (
    "'automatic', 'reconciled_applied', 'reconciled_not_applied', 'reconciled_abandon'"
)
_KINDS_0004 = "'advance_run', 'execute_tool', 'expire_approval', 'run_deadline'"

UPGRADE = [
    "ALTER TABLE tether.ledger_entries ADD COLUMN verification_attempts integer NOT NULL DEFAULT 0",
    "ALTER TABLE tether.ledger_entries ADD COLUMN verification_evidence_masked jsonb",
    "ALTER TABLE tether.ledger_entries ADD CONSTRAINT ck_ledger_entries_verification_attempts"
    " CHECK (verification_attempts >= 0)",
    "ALTER TABLE tether.ledger_entries DROP CONSTRAINT ck_ledger_entries_resolution",
    "ALTER TABLE tether.ledger_entries ADD CONSTRAINT ck_ledger_entries_resolution"
    f" CHECK (resolution IS NULL OR resolution IN ({_RESOLUTIONS_0004},"
    " 'verified_applied', 'verified_not_applied'))",
    "ALTER TABLE tether.ledger_entries ADD CONSTRAINT ck_ledger_entries_verification"
    " CHECK (resolution IS NULL"
    " OR resolution NOT IN ('verified_applied', 'verified_not_applied')"
    " OR (resolved_by IS NOT NULL AND verification_evidence_masked IS NOT NULL))",
    "ALTER TABLE tether.jobs DROP CONSTRAINT ck_jobs_kind",
    f"ALTER TABLE tether.jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN ({_KINDS_0004},"
    " 'verify_outcome'))",
]

DOWNGRADE = [
    "ALTER TABLE tether.jobs DROP CONSTRAINT ck_jobs_kind",
    f"ALTER TABLE tether.jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN ({_KINDS_0004}))",
    "ALTER TABLE tether.ledger_entries DROP CONSTRAINT ck_ledger_entries_verification",
    "ALTER TABLE tether.ledger_entries DROP CONSTRAINT ck_ledger_entries_resolution",
    "ALTER TABLE tether.ledger_entries ADD CONSTRAINT ck_ledger_entries_resolution"
    f" CHECK (resolution IS NULL OR resolution IN ({_RESOLUTIONS_0004}))",
    "ALTER TABLE tether.ledger_entries DROP CONSTRAINT ck_ledger_entries_verification_attempts",
    "ALTER TABLE tether.ledger_entries DROP COLUMN verification_evidence_masked",
    "ALTER TABLE tether.ledger_entries DROP COLUMN verification_attempts",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
