"""HTTP client for the demo target. Forwards the Tether idempotency key on writes."""

from __future__ import annotations

import os

import httpx

_transport: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests route requests to an in-process app instead of the network."""
    global _transport
    _transport = transport


def http_client() -> httpx.AsyncClient:
    base_url = os.environ.get("DEMO_TARGET_URL", "http://demo-target:8000")
    return httpx.AsyncClient(base_url=base_url, transport=_transport, timeout=10.0)
