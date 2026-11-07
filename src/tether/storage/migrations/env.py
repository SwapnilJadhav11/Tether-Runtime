"""Alembic environment for the ``tether`` schema (PD-4).

The database URL comes from ``config.attributes["database_url"]`` when invoked through
``tether migrate``, otherwise from ``TETHER_MIGRATOR_DATABASE_URL`` (for the ``alembic`` CLI).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy.engine import make_url

from tether.settings import MigrationSettings
from tether.storage.engine import create_migration_engine
from tether.storage.tables import MIGRATION_CONTEXT_OPTIONS, metadata


def _database_url() -> str:
    configured = context.config.attributes.get("database_url")
    raw = (
        configured
        if isinstance(configured, str)
        else MigrationSettings().migrator_database_url.get_secret_value()
    )
    # One driver everywhere (PD-2): SQLAlchemy talks to Postgres through psycopg 3.
    return make_url(raw).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=metadata,
        literal_binds=True,
        **MIGRATION_CONTEXT_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_migration_engine(_database_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            **MIGRATION_CONTEXT_OPTIONS,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
