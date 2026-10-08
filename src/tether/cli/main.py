"""The ``tether`` command-line interface."""

from __future__ import annotations

import typer
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from tether.settings import MigrationSettings

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Tether administration.")

MIGRATIONS_LOCATION = "tether.storage:migrations"


@app.callback()
def _root() -> None:
    """Tether administration."""


def upgrade_to_head(dsn: str) -> None:
    """Apply every pending migration to the ``tether`` schema. Safe to run repeatedly."""
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    config.attributes["database_url"] = dsn
    command.upgrade(config, "head")


@app.command()
def migrate() -> None:
    """Apply Tether schema migrations (PD-4). Idempotent."""
    try:
        settings = MigrationSettings()
    except ValidationError:
        typer.echo("TETHER_MIGRATOR_DATABASE_URL must be set to the migrator role's URL.", err=True)
        raise typer.Exit(code=2) from None
    upgrade_to_head(settings.migrator_database_url.get_secret_value())
    typer.echo("migrations applied")


if __name__ == "__main__":
    app()
