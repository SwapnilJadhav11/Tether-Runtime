"""``tether audit verify``: recompute one run's audit chain from the authoritative database.

A read-only operator command (ADR-0015, P-9). It runs as the application role, so it needs
``TETHER_DATABASE_URL`` and the audit key family (``TETHER_AUDIT_KEY_<ID>``) in the environment.
The workspace is always explicit: a run is looked up inside one workspace, and a run that is
unknown or belongs to another workspace is the same ``NOT_FOUND`` (ADR-0013).

Exit codes:

- 0  ``VALID``: the chain recomputes and the persisted head agrees with it.
- 1  ``INVALID``: the stored data fail verification (the reason is a stable code).
- 2  usage or configuration error (nothing was verified).
- 3  operational failure: the database or an audit key was unavailable, so nothing can be said
     about the chain. This is never reported as ``INVALID``.
- 4  ``NOT_FOUND``: no events for that run in that workspace.

The output carries identifiers, counts and codes only: never payloads, MACs, keys or connection
details. V1 limit (ADR-0015 rule 8): deleting a run's tail *and* rewriting its head, or deleting
the whole run, is not detected.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Final

import typer
from pydantic import ValidationError

from tether.audit.chain import ChainVerification, verify_run_report
from tether.core.errors import InvalidKeyMaterialError, KeyUnavailableError
from tether.core.ids import WorkspaceId
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyRing
from tether.settings import Settings
from tether.storage.engine import create_database_engine
from tether.storage.repos.audit import AuditRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

EXIT_VALID: Final = 0
EXIT_INVALID: Final = 1
EXIT_USAGE: Final = 2
EXIT_OPERATIONAL: Final = 3
EXIT_NOT_FOUND: Final = 4

app = typer.Typer(no_args_is_help=True, help="Audit chain tooling.")


@app.callback()
def _root() -> None:
    """Audit chain tooling."""


async def _load_report(workspace: str, run_id: uuid.UUID) -> ChainVerification:
    """Verify one run through the authoritative verifier, in one database snapshot."""
    settings = Settings()
    engine = create_database_engine(settings.database_url.get_secret_value())
    # One snapshot for the head and the events, so a run being written to cannot look torn.
    engine = engine.execution_options(isolation_level="REPEATABLE READ")
    try:
        async with UnitOfWork(engine) as uow:
            repo = uow.repository(AuditRepository, WorkspaceScope(WorkspaceId(workspace)))
            return await verify_run_report(repo, KeyRing(EnvSecretsProvider()), run_id)
    finally:
        await engine.dispose()


def _echo_identity(label: str, workspace: str, run_id: uuid.UUID) -> None:
    typer.echo(label)
    typer.echo(f"workspace: {workspace}")
    typer.echo(f"run: {run_id}")


@app.command()
def verify(
    workspace: str = typer.Option(..., "--workspace", help="The workspace that owns the run."),
    run: str = typer.Option(..., "--run", help="The run ID (UUID)."),
) -> None:
    """Verify RUN's audit chain inside WORKSPACE. Exit 0 only if it is VALID."""
    try:
        run_id = uuid.UUID(run)
    except ValueError:
        typer.echo("--run must be a UUID.", err=True)
        raise typer.Exit(code=EXIT_USAGE) from None
    if not workspace.strip():
        typer.echo("--workspace must not be empty.", err=True)
        raise typer.Exit(code=EXIT_USAGE)
    try:
        Settings()
    except ValidationError:
        typer.echo("TETHER_DATABASE_URL must be set to the application role's URL.", err=True)
        raise typer.Exit(code=EXIT_USAGE) from None

    try:
        # psycopg's async driver cannot use the Windows default (Proactor) event loop.
        result = asyncio.run(
            _load_report(workspace, run_id), loop_factory=asyncio.SelectorEventLoop
        )
    except (KeyUnavailableError, InvalidKeyMaterialError):
        typer.echo("audit key unavailable: the chain could not be checked.", err=True)
        raise typer.Exit(code=EXIT_OPERATIONAL) from None
    except Exception as error:
        # Any other failure is operational, never "INVALID". Print the type only: messages from
        # database drivers can carry connection details.
        typer.echo(f"verification could not be completed ({type(error).__name__}).", err=True)
        raise typer.Exit(code=EXIT_OPERATIONAL) from None

    if result.event_count == 0 and result.head_seq is None:
        _echo_identity("NOT_FOUND", workspace, run_id)
        raise typer.Exit(code=EXIT_NOT_FOUND)
    if not result.valid:
        _echo_identity("INVALID", workspace, run_id)
        typer.echo(f"reason: {result.reason}")
        if result.failed_seq is not None:
            typer.echo(f"failed_seq: {result.failed_seq}")
        typer.echo(f"events: {result.event_count}")
        raise typer.Exit(code=EXIT_INVALID)
    _echo_identity("VALID", workspace, run_id)
    typer.echo(f"events: {result.event_count}")
    typer.echo(f"last_seq: {result.last_seq}")
    typer.echo(f"head: seq={result.head_seq} consistent")
    typer.echo(f"mac_versions: {','.join(str(v) for v in result.mac_versions)}")
