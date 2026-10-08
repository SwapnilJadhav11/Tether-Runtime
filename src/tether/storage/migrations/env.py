"""Alembic environment for the ``tether`` schema (PD-4).

The database URL comes from ``config.attributes["database_url"]`` when invoked through
``tether migrate``, otherwise from ``TETHER_MIGRATOR_DATABASE_URL`` (for the ``alembic`` CLI).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from tether.settings import MigrationSettings

TETHER_SCHEMA = "tether"


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
        target_metadata=None,
        version_table_schema=TETHER_SCHEMA,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table_schema=TETHER_SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
