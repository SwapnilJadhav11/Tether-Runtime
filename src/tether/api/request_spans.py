"""One server span per API request (S7.3), as pure ASGI middleware.

The span is current while the request is handled, so TX1 can persist it as the run's root trace
context. It is named by method and route *template* (``POST /v1/tasks/runs/{run_id}/cancel``),
never by the concrete path, and records only the method, the route template and the status code:
no headers (bearer tokens, API keys), no query string, no body. Health probes get no span.
"""

from __future__ import annotations

import contextlib
from typing import Any

from opentelemetry.trace import SpanKind
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tether.observability import tracing

_UNTRACED = frozenset({"/healthz", "/readyz"})
_SERVER_ERROR = 500


class RequestSpans:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _UNTRACED:
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "HTTP"))
        status: dict[str, int] = {}

        async def record_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        with tracing.span(
            method,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method},
        ) as span:
            try:
                await self.app(scope, receive, record_status)
            finally:
                route = _route_template(scope)
                # A span name is never worth failing a request.
                with contextlib.suppress(Exception):
                    span.update_name(f"{method} {route}" if route else method)
                tracing.annotate(
                    span,
                    **{"http.route": route, "http.response.status_code": status.get("code")},
                )
                if status.get("code", _SERVER_ERROR) >= _SERVER_ERROR:
                    tracing.mark_error(span, "server error")


def _route_template(scope: Scope) -> str | None:
    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else None
