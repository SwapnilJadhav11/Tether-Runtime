"""Demo target: one controllable service with an idempotent restart endpoint.

Tether never imports this (ADR-0002); the reference workload calls it over HTTP.
MVP scope: state and idempotency records live in memory (a restart of this process resets them).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException


def create_app() -> FastAPI:
    app = FastAPI(title="demo target", docs_url=None, redoc_url=None)
    services: dict[str, dict[str, Any]] = {"checkout": {"healthy": False, "restarts": 0}}
    applied: dict[str, tuple[str, dict[str, Any]]] = {}

    def view(name: str) -> dict[str, Any]:
        if name not in services:
            raise HTTPException(404, "unknown service")
        return {"service": name, **services[name]}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/services/{name}")
    async def status(name: str) -> dict[str, Any]:
        return view(name)

    @app.post("/services/{name}/restart")
    async def restart(
        name: str, idempotency_key: Annotated[str, Header(min_length=1)]
    ) -> dict[str, Any]:
        # No await between the check and the write: a single event loop makes this atomic.
        if idempotency_key in applied:
            original_name, response = applied[idempotency_key]
            if original_name != name:
                raise HTTPException(422, "idempotency key reused for a different request")
            return response
        view(name)
        services[name]["restarts"] += 1
        services[name]["healthy"] = True
        response = view(name)
        applied[idempotency_key] = (name, response)
        return response

    return app


app = create_app()
