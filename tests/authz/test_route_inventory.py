"""T-24 route-inventory guard (plan S7.2): no route can exist without a row in the matrix.

A new route that is not in ``matrix.py`` (or declared public there) fails this test, so adding an
endpoint forces its authorization table to be written. The guard is itself tested: it must be able
to fail, in both directions, and no route may create or alter workspaces, issuers, keys, agents or
policy (ADR-0014: platform administration has no runtime API).
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.routing import BaseRoute
from tests.authz.fixtures import APPROVALS, OPS, TASKS
from tests.authz.matrix import MATRIX_ROUTES, PUBLIC_ROUTES, ROUTES

from tether.api.app import create_app
from tether.settings import Settings

AUDIENCE_BY_PREFIX = {"/v1/tasks": TASKS, "/v1/approvals": APPROVALS, "/v1/ops": OPS}
# Path segments that would mean a route administers the platform (ADR-0014).
ADMIN_SEGMENTS = frozenset(
    {
        "workspaces",
        "workspace",
        "issuers",
        "issuer",
        "keys",
        "api-keys",
        "apikeys",
        "api_keys",
        "agents",
        "agent",
        "policy",
        "policies",
        "bootstrap",
        "admin",
        "users",
    }
)
Route = tuple[str, str]


def app_under_test() -> FastAPI:
    # No lifespan is entered: building the app touches neither the database nor the network.
    return create_app(Settings(database_url=SecretStr("postgresql://x:y@localhost/z"), env="test"))


def declared_routes(routes: Iterable[BaseRoute]) -> set[Route]:
    """Every (method, path) the application serves.

    FastAPI wraps each included router in a lazy node that exposes its flattened routes through
    ``effective_route_contexts()``; those are followed. Anything that serves requests without
    saying which methods it accepts (a mounted application, a route type this walker does not
    know) counts as ``*``, which no matrix row can cover: it fails the guard instead of hiding.
    """
    found: set[Route] = set()
    for route in routes:
        flattened = getattr(route, "effective_route_contexts", None)
        if callable(flattened):
            found |= declared_routes(flattened())
            continue
        path = getattr(route, "path_format", None) or getattr(route, "path", None) or "<unknown>"
        methods = getattr(route, "methods", None) or {"*"}
        found.update((method, path) for method in methods - {"HEAD", "OPTIONS"})
    return found


def inventory_gaps(
    declared: set[Route], matrix: Iterable[Route], public: Iterable[Route]
) -> tuple[set[Route], set[Route]]:
    """(routes served but not in the matrix, matrix or public entries that are not served)."""
    covered = set(matrix) | set(public)
    return declared - covered, covered - declared


def test_t24_every_route_the_application_serves_has_a_row_in_the_matrix() -> None:
    served = declared_routes(app_under_test().routes)
    unlisted, stale = inventory_gaps(served, MATRIX_ROUTES, PUBLIC_ROUTES)
    assert not unlisted, f"routes without an authorization table row: {sorted(unlisted)}"
    assert not stale, f"matrix rows for routes that do not exist: {sorted(stale)}"


def test_t24_the_guard_fails_for_a_route_missing_from_the_matrix() -> None:
    """Negative self-test: the guard must be able to fail, or it proves nothing."""
    app = app_under_test()

    @app.get("/v1/ops/audit")
    async def audit_query() -> dict[str, str]:  # a new endpoint nobody put in the matrix
        return {}

    unlisted, stale = inventory_gaps(declared_routes(app.routes), MATRIX_ROUTES, PUBLIC_ROUTES)
    assert unlisted == {("GET", "/v1/ops/audit")}
    assert not stale


def test_t24_the_guard_fails_for_a_matrix_row_without_a_route() -> None:
    served = declared_routes(app_under_test().routes)
    unlisted, stale = inventory_gaps(
        served, {*MATRIX_ROUTES, ("DELETE", "/v1/tasks/runs/{run_id}")}, PUBLIC_ROUTES
    )
    assert not unlisted
    assert stale == {("DELETE", "/v1/tasks/runs/{run_id}")}


def test_t24_the_guard_fails_when_a_route_loses_its_row() -> None:
    served = declared_routes(app_under_test().routes)
    removed = ("GET", "/v1/ops/runs/{run_id}")
    unlisted, _ = inventory_gaps(served, MATRIX_ROUTES - {removed}, PUBLIC_ROUTES)
    assert unlisted == {removed}


def test_t24_the_guard_sees_other_methods_mounted_apps_and_wildcards() -> None:
    app = app_under_test()

    @app.delete("/v1/tasks/runs/{run_id}")
    async def delete_run(run_id: str) -> None: ...

    app.mount("/sidecar", FastAPI())
    unlisted, _ = inventory_gaps(declared_routes(app.routes), MATRIX_ROUTES, PUBLIC_ROUTES)
    assert ("DELETE", "/v1/tasks/runs/{run_id}") in unlisted
    assert [route for route in unlisted if route[0] == "*" and route[1].startswith("/sidecar")]


def test_t24_no_route_administers_workspaces_issuers_keys_agents_or_policy() -> None:
    """ADR-0014: platform administration is bootstrap YAML and the CLI, never a runtime route."""
    for method, path in declared_routes(app_under_test().routes):
        segments = {part.lower() for part in path.split("/") if part and not part.startswith("{")}
        assert not segments & ADMIN_SEGMENTS, f"{method} {path} looks like an admin route"
    # The guard itself can fail: a made-up admin route is caught.
    app = app_under_test()

    @app.post("/v1/ops/workspaces")
    async def create_workspace() -> None: ...

    flagged = [
        (method, path)
        for method, path in declared_routes(app.routes)
        if {p.lower() for p in path.split("/") if p} & ADMIN_SEGMENTS
    ]
    assert flagged == [("POST", "/v1/ops/workspaces")]


def test_t24_the_matrix_ties_each_route_to_the_audience_of_its_prefix() -> None:
    for spec in ROUTES:
        expected = next(
            a for prefix, a in AUDIENCE_BY_PREFIX.items() if spec.path.startswith(prefix)
        )
        assert spec.audience == expected, spec.label


def test_t24_only_the_probes_and_the_api_schema_are_public() -> None:
    assert {("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/openapi.json")} == PUBLIC_ROUTES
    assert not any(path.startswith("/v1/") for _, path in PUBLIC_ROUTES)


@pytest.mark.parametrize("spec", ROUTES, ids=lambda spec: spec.label)
def test_t24_every_matrix_route_states_a_status_for_every_identity(spec: object) -> None:
    from tests.authz.fixtures import WORKSPACES
    from tests.authz.matrix import IDENTITIES, RouteSpec, cases

    assert isinstance(spec, RouteSpec)
    assert set(spec.allow) == set(IDENTITIES)
    for workspace in WORKSPACES:
        generated = cases(spec, workspace)
        assert any(case.permitted for case in generated)
        assert any(not case.permitted for case in generated)
