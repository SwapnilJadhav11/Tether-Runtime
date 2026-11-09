"""``tether bootstrap apply``: write bootstrap config to Postgres in one transaction (ADR-0014)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import typer
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from tether.policy.config import (
    BootstrapConfigError,
    BootstrapFile,
    WorkspaceConfig,
    load_bootstrap,
)
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
            _apply_identity(conn, ws)
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


def _apply_identity(conn: psycopg.Connection[Any], ws: WorkspaceConfig) -> None:
    """The workspace's issuers, API keys and agent bindings become exactly what the file says.

    Removing an issuer or a key from the file and applying revokes it (PD-20 rotation by overlap).
    """
    key_ids = [key.key_id for key in ws.api_keys]
    conn.execute("DELETE FROM tether.api_key_agents WHERE workspace_id = %s", (ws.id,))
    conn.execute(
        "DELETE FROM tether.api_keys WHERE workspace_id = %s AND NOT (key_id = ANY(%s))",
        (ws.id, key_ids),
    )
    for key in ws.api_keys:
        changed = conn.execute(
            "INSERT INTO tether.api_keys (key_id, workspace_id, salt, secret_hash)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (key_id) DO UPDATE"
            " SET salt = EXCLUDED.salt, secret_hash = EXCLUDED.secret_hash"
            " WHERE tether.api_keys.workspace_id = EXCLUDED.workspace_id RETURNING key_id",
            (key.key_id, ws.id, bytes.fromhex(key.salt), bytes.fromhex(key.secret_hash)),
        ).fetchone()
        if changed is None:
            raise BootstrapConfigError(f"api key {key.key_id} belongs to another workspace")
        for agent_id in key.agents:
            conn.execute(
                "INSERT INTO tether.api_key_agents (workspace_id, key_id, agent_id)"
                " VALUES (%s, %s, %s)",
                (ws.id, key.key_id, agent_id),
            )
    issuer_ids = [issuer.id for issuer in ws.issuers]
    conn.execute(
        "DELETE FROM tether.issuers WHERE workspace_id = %s AND NOT (issuer_id = ANY(%s))",
        (ws.id, issuer_ids),
    )
    for issuer in ws.issuers:
        conn.execute(
            "INSERT INTO tether.issuers"
            " (workspace_id, issuer_id, iss, audiences, algorithm, public_keys)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (workspace_id, issuer_id) DO UPDATE"
            " SET iss = EXCLUDED.iss, audiences = EXCLUDED.audiences,"
            " algorithm = EXCLUDED.algorithm, public_keys = EXCLUDED.public_keys",
            (
                ws.id,
                issuer.id,
                issuer.iss,
                list(issuer.audiences),
                issuer.algorithm,
                Jsonb(dict(issuer.public_keys)),
            ),
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
