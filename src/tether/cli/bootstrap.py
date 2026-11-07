"""``tether bootstrap apply``: write bootstrap config to Postgres in one transaction (ADR-0014)."""

from __future__ import annotations

from pathlib import Path

import psycopg
import typer
from pydantic import ValidationError

from tether.policy.config import BootstrapConfigError, BootstrapFile, load_bootstrap
from tether.settings import MigrationSettings

app = typer.Typer(no_args_is_help=True, help="Apply bootstrap configuration.")


def apply_bootstrap(dsn: str, config: BootstrapFile) -> None:
    """Idempotent: re-applying the same file changes nothing. All or nothing."""
    with psycopg.connect(dsn) as conn, conn.transaction():
        for ws in config.workspaces:
            conn.execute(
                "INSERT INTO tether.workspaces (id) VALUES (%s) ON CONFLICT DO NOTHING", (ws.id,)
            )
            for agent in ws.agents:
                conn.execute(
                    "INSERT INTO tether.agents (workspace_id, agent_id) VALUES (%s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (ws.id, agent.id),
                )
            version = ws.policy_version
            conn.execute(
                "INSERT INTO tether.policy_versions (workspace_id, version) VALUES (%s, %s)"
                " ON CONFLICT DO NOTHING",
                (ws.id, version),
            )
            # Deactivate first: at most one active version per workspace (partial unique index).
            conn.execute(
                "UPDATE tether.policy_versions SET active = false"
                " WHERE workspace_id = %s AND version <> %s AND active",
                (ws.id, version),
            )
            conn.execute(
                "UPDATE tether.policy_versions SET active = true"
                " WHERE workspace_id = %s AND version = %s AND NOT active",
                (ws.id, version),
            )


@app.command()
def apply(path: Path) -> None:
    """Validate PATH and apply it as the migrator role."""
    try:
        settings = MigrationSettings()
    except ValidationError:
        typer.echo("TETHER_MIGRATOR_DATABASE_URL must be set to the migrator role's URL.", err=True)
        raise typer.Exit(code=2) from None
    try:
        config = load_bootstrap(path)
    except BootstrapConfigError as error:
        typer.echo(f"invalid bootstrap file: {error}", err=True)
        raise typer.Exit(code=1) from None
    apply_bootstrap(settings.migrator_database_url.get_secret_value(), config)
    typer.echo(f"bootstrap applied: {len(config.workspaces)} workspace(s)")
