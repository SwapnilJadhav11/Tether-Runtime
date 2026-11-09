"""``tether apikey new``: issue a workspace API key (PD-20, ADR-0014).

Prints the raw key once, for the operator to hand to the calling application, and the bootstrap
lines (key ID, salt, SHA-256 hash) to paste under the workspace's ``api_keys``. Tether stores
only the hash; the raw key is never written anywhere by this command.
"""

from __future__ import annotations

import typer

from tether.identity import api_keys

app = typer.Typer(no_args_is_help=True, help="Issue workspace API keys.")


@app.callback()
def _root() -> None:
    """Issue workspace API keys."""


@app.command()
def new(key_id: str, agents: list[str] = typer.Option(..., "--agent")) -> None:  # noqa: B008
    """Issue KEY_ID bound to the given agents (repeat --agent)."""
    try:
        issued = api_keys.generate(key_id)
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from None
    typer.echo("Raw API key (shown once; give it to the calling app, never commit it):")
    typer.echo(f"  {issued.raw}")
    typer.echo("Bootstrap entry for the workspace's api_keys:")
    typer.echo(f"  - key_id: {issued.key_id}")
    typer.echo(f"    salt: {issued.salt.hex()}")
    typer.echo(f"    secret_hash: {issued.secret_hash.hex()}")
    typer.echo(f"    agents: [{', '.join(agents)}]")
