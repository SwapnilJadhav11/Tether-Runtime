"""Baseline: empty ``tether`` schema.

The schema, roles and grants are created by the Postgres init script. Tables arrive in S1.5.

Revision ID: 0001
Revises:
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
