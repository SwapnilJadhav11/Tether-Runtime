"""The T-16 sink scanner (W10.2): look for canary values in every sink that exists today.

``scan`` is pure: it takes the text of each named sink and the canaries, and reports every
(canary, form, sink) it finds, minus the sinks ADR-0017 §2 names as exceptions for a canary. A
sink is one string: a span's JSON, the whole log text, one table's rows as JSON, one route
response, one provider history. Canaries are searched raw, hex-encoded (how a ``bytea`` column
appears in ``row_to_json``) and base64-encoded.

The collectors read the real thing: every table of the ``tether`` and ``langgraph`` schemas
(whole rows, not selected columns), and every route the app exposes.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg

from tether.api.app import create_app


def forms(canary: str) -> dict[str, str]:
    raw = canary.encode()
    return {
        "raw": canary,
        "hex": raw.hex(),
        "base64": base64.b64encode(raw).decode(),
        "base64url": base64.urlsafe_b64encode(raw).decode(),
    }


def scan(
    sinks: Mapping[str, str],
    canaries: Mapping[str, str],
    exempt: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Every place a canary appears, as ``"<name> (<form>) in <sink>"``.

    ``exempt`` maps a canary name to sink-name prefixes where that canary is allowed to appear
    (ADR-0017 §2: unmasked arguments in the checkpoint, the job payload and the model's own
    history). Everything else is a finding.
    """
    allowed = exempt or {}
    found: list[str] = []
    for sink, text in sinks.items():
        for name, canary in canaries.items():
            if any(sink.startswith(prefix) for prefix in allowed.get(name, ())):
                continue
            for form, needle in forms(canary).items():
                if needle in text:
                    found.append(f"{name} ({form}) in {sink}")
    return found


# --- database sinks --------------------------------------------------------------------------


def table_sinks(migrator_dsn: str) -> dict[str, str]:
    """Whole-row JSON text of every table in the Tether and LangGraph schemas.

    Named ``table:tether.<name>`` and ``checkpoint:langgraph.<name>``. The database belongs to
    one test, so this covers the run under test and nothing else.
    """
    sinks: dict[str, str] = {}
    with psycopg.connect(migrator_dsn) as conn:
        tables = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables"
            " WHERE table_schema IN ('tether', 'langgraph') AND table_type = 'BASE TABLE'"
            " ORDER BY 1, 2"
        ).fetchall()
        for schema, name in tables:
            [(text,)] = conn.execute(
                f"SELECT coalesce(string_agg(row_to_json(t)::text, E'\\n'), '') "  # noqa: S608
                f'FROM "{schema}"."{name}" t'
            ).fetchall()
            kind = "checkpoint" if schema == "langgraph" else "table"
            sinks[f"{kind}:{schema}.{name}"] = str(text)
    return sinks


# --- routes ----------------------------------------------------------------------------------

# The only non-GET routes the scan knows how to drive (each is called in the test with a body).
KNOWN_WRITES = {
    ("POST", "/v1/tasks/runs"),
    ("POST", "/v1/tasks/runs/{run_id}/cancel"),
    ("POST", "/v1/approvals/{approval_id}/{action}"),
    ("POST", "/v1/ops/ledger/{ledger_id}/reconcile"),
    ("POST", "/v1/ops/runs/{run_id}/cancel"),
}


@dataclass(frozen=True)
class RouteInventory:
    gets: list[str]  # path templates, each parameter in braces
    unknown: list[str]  # "METHOD path" of anything the scan cannot drive


def inventory(settings: Any) -> RouteInventory:
    """Every ``/v1`` route of the app, split into GETs (called generically) and the rest."""
    # The OpenAPI document is the public, router-nesting-independent list of every route.
    paths: dict[str, Any] = create_app(settings).openapi()["paths"]
    gets: list[str] = []
    unknown: list[str] = []
    for path, operations in paths.items():
        if not path.startswith("/v1"):
            continue
        for method in (name.upper() for name in operations):
            if method == "GET":
                gets.append(path)
            elif (method, path) not in KNOWN_WRITES:
                unknown.append(f"{method} {path}")
    return RouteInventory(gets=sorted(gets), unknown=sorted(unknown))


def fill(path: str, values: Mapping[str, uuid.UUID]) -> str | None:
    """``path`` with every ``{name}`` replaced from ``values``; ``None`` if one is unknown."""
    out = path
    for name in _params(path):
        if name not in values:
            return None
        out = out.replace("{" + name + "}", str(values[name]))
    return out


def _params(path: str) -> list[str]:
    return [part[1:-1] for part in path.split("/") if part.startswith("{") and part.endswith("}")]


class RouteSinks:
    """Collects the text of every response a test provokes, one sink per call."""

    def __init__(self) -> None:
        self.sinks: dict[str, str] = {}

    def record(self, label: str, response: httpx.Response) -> httpx.Response:
        key = f"route:{label}"
        while key in self.sinks:
            key += "+"
        self.sinks[key] = f"{response.status_code}\n{dict(response.headers)}\n{response.text}"
        return response
