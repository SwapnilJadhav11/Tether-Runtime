"""The ``tether`` command-line interface."""

from __future__ import annotations

from typing import Any

import typer
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from typer.core import TyperGroup

from tether.cli import apikey, bootstrap
from tether.cli import audit as audit_cli
from tether.runtime.checkpointer import setup_langgraph_schema
from tether.settings import MigrationSettings, require_explicit_environment

# Every command or group that connects to the database. `apikey` only prints a new key.
DATABASE_COMMANDS = frozenset({"migrate", "bootstrap", "audit"})


class _EnvironmentGate(TyperGroup):
    """W7.1 (D-12): a command that connects to the database needs an explicit, valid TETHER_ENV.

    Checked here, once, before the command is dispatched, so no database command can forget it.
    Help never connects: ``--help`` (and a bare group, which prints its help) is not gated.
    """

    def resolve_command(self, ctx: Any, args: list[str]) -> tuple[str | None, Any, list[str]]:
        name, found, rest = super().resolve_command(ctx, args)
        shows_help = "--help" in rest or (isinstance(found, TyperGroup) and not rest)
        if name in DATABASE_COMMANDS and not shows_help:
            require_explicit_environment()
        return name, found, rest


app = typer.Typer(
    cls=_EnvironmentGate,
    no_args_is_help=True,
    add_completion=False,
    help="Tether administration.",
)
app.add_typer(bootstrap.app, name="bootstrap")
app.add_typer(apikey.app, name="apikey")
app.add_typer(audit_cli.app, name="audit")

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
    """Apply the `tether` schema migrations and the LangGraph saver setup (PD-4). Idempotent."""
    try:
        settings = MigrationSettings()
    except ValidationError:
        typer.echo("TETHER_MIGRATOR_DATABASE_URL must be set to the migrator role's URL.", err=True)
        raise typer.Exit(code=2) from None
    dsn = settings.migrator_database_url.get_secret_value()
    upgrade_to_head(dsn)
    setup_langgraph_schema(dsn)
    typer.echo("migrations applied")


if __name__ == "__main__":
    app()
