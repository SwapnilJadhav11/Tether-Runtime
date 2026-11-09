"""FastAPI application factory and the ``tether-api`` entrypoint.

Serves the unauthenticated liveness and readiness probes, which return no data, and the
``/v1/tasks`` router.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tether.api.request_spans import RequestSpans
from tether.api.routers import approvals, ops, tasks
from tether.observability.telemetry import setup_telemetry
from tether.policy.config import BootstrapFile, load_bootstrap
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyRing
from tether.settings import Settings
from tether.storage.engine import create_database_engine, create_pool, database_is_reachable


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        dsn = resolved.database_url.get_secret_value()
        pool = create_pool(dsn, name="tether-api")
        await pool.open(wait=False)
        app.state.db_pool = pool
        app.state.db_engine = create_database_engine(dsn)
        try:
            yield
        finally:
            await app.state.db_engine.dispose()
            await pool.close()

    app = FastAPI(
        title="Tether",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        # FastAPI's own telemetry: keep its bounded HTTP metrics (route template, method,
        # status); request spans come from ``RequestSpans`` instead, which never continues a
        # caller-supplied trace (AD-13: the run's trace is created at submission) and never
        # records the query string. No per-dependency spans, no exception logs outside the
        # scrubber, and no second exporter configuration from OTEL_* variables.
        telemetry={
            "tracing": False,
            "operation_spans": False,
            "logs": False,
            "auto_configure": False,
        },
    )
    app.state.settings = resolved
    app.state.keyring = KeyRing(EnvSecretsProvider())
    app.state.bootstrap = (
        load_bootstrap(resolved.bootstrap_file) if resolved.bootstrap_file else BootstrapFile()
    )
    app.include_router(tasks.router)
    app.include_router(approvals.router)
    app.include_router(ops.router)
    app.add_middleware(RequestSpans)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is serving requests. Does not touch the database."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: the database is reachable with the application role."""
        if await database_is_reachable(
            request.app.state.db_pool, timeout=resolved.readiness_timeout_seconds
        ):
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "unavailable"}, status_code=503)

    return app


def run() -> None:
    """Entrypoint for the ``api`` process."""
    settings = Settings()
    telemetry = setup_telemetry(settings, service="tether-api")
    try:
        # ``log_config=None``: uvicorn's records go through the JSON pipeline set up above.
        uvicorn.run(
            create_app(settings), host=settings.api_host, port=settings.api_port, log_config=None
        )
    finally:
        telemetry.shutdown()
