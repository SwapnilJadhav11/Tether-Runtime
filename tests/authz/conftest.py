"""Authorization tests reuse the integration database fixtures: a migrated template per test."""

from __future__ import annotations

from tests.authz.fixtures import world
from tests.integration.conftest import fresh_db, rows, seeded, template_database

__all__ = ["fresh_db", "rows", "seeded", "template_database", "world"]
