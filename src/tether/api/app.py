"""FastAPI application factory and the ``tether-api`` entrypoint.

S1.2 serves only the unauthenticated liveness and readiness probes, which return no data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tether.settings import Settings
from tether.storage.engine import create_pool, database_is_reachable


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = create_pool(resolved.database_url.get_secret_value(), name="tether-api")
        await pool.open(wait=False)
        app.state.db_pool = pool
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title="Tether", lifespan=lifespan, docs_url=None, redoc_url=None)

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
    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port)
