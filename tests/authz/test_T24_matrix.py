"""T-24: every route, audience, role, workspace and credential state, against the PRD table.

The expected statuses are data (``matrix.py``); this test only fires them. Each route gets one
world with runs, approvals and ledger entries in both workspaces (``ops-demo`` and ``fixture-b``),
and every case is also fired in the mirror direction, so neither workspace can reach the other's
data. Refusals go first and must change nothing; permitted mutations then each consume a target
of their own.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from tests.authz.fixtures import WORKSPACES, World
from tests.authz.matrix import ROUTES, Case, RouteSpec, cases

from tether.audit.access_events import writer_for


class Targets:
    """Runs, approvals and ledger entries a case can aim at, created through the real API."""

    def __init__(self, world: World) -> None:
        self._world = world
        self._shared: dict[tuple[str, str], uuid.UUID] = {}

    async def fresh(self, kind: str, ws: str) -> uuid.UUID:
        run = await self._world.submit(ws, "nonidem" if kind == "ledger" else "restart")
        if kind == "approval":
            return self._world.approval_id(run)
        if kind == "ledger":
            return self._world.ledger_id(run)
        return run

    async def shared(self, kind: str, ws: str) -> uuid.UUID:
        if (kind, ws) not in self._shared:
            self._shared[(kind, ws)] = await self.fresh(kind, ws)
        return self._shared[(kind, ws)]


def url_for(spec: RouteSpec, target: uuid.UUID | None) -> str:
    ids = {"run": "run_id", "approval": "approval_id", "ledger": "ledger_id"}
    params = dict(spec.fixed)
    if spec.target is not None:
        params[ids[spec.target]] = str(target)
    return spec.path.format(**params)


@pytest.mark.parametrize("spec", ROUTES, ids=lambda spec: spec.label)
@pytest.mark.asyncio
async def test_t24_every_credential_state_gets_the_status_the_table_demands(
    world: World, spec: RouteSpec
) -> None:
    targets = Targets(world)
    plan: list[tuple[Case, uuid.UUID | None]] = []
    for target_ws in WORKSPACES:
        shared = await targets.shared(spec.target, target_ws) if spec.target else None
        for case in cases(spec, target_ws):
            if case.unknown_target:
                plan.append((case, uuid.uuid4()))
            elif case.permitted and spec.method == "POST" and spec.target:
                # A permitted mutation uses up its target, so each one gets its own.
                plan.append((case, await targets.fresh(spec.target, target_ws)))
            else:
                plan.append((case, shared))
    refused = [item for item in plan if not item[0].permitted]
    permitted = [item for item in plan if item[0].permitted]
    assert refused, f"{spec.label}: the table must refuse something"
    assert permitted, f"{spec.label}: the table must permit something"
    mismatches: list[str] = []
    async with world.serving() as (app, http):
        before = world.counts()
        for case, target in refused:
            await _fire(http, spec, case, target, mismatches)
        assert world.counts() == before, f"{spec.label}: a refused request changed state"
        for case, target in permitted:
            await _fire(http, spec, case, target, mismatches)
        await writer_for(app.state).aclose()
    assert not mismatches, "\n".join(mismatches)


async def _fire(
    http: httpx.AsyncClient,
    spec: RouteSpec,
    case: Case,
    target: uuid.UUID | None,
    mismatches: list[str],
) -> None:
    response = await http.request(
        spec.method, url_for(spec, target), headers=case.headers(), json=spec.body
    )
    if response.status_code != case.expected:
        mismatches.append(
            f"{spec.label} on {case.target_ws} data, {case.what} ({case.credential}, "
            f"{case.audience} token of {case.cred_ws}): expected {case.expected}, "
            f"got {response.status_code} {response.text[:120]}"
        )
    elif response.status_code in (401, 404):
        # Out-of-scope and unauthenticated answers say nothing about what exists.
        body = response.json()
        if body not in ({"detail": "Not Found"}, {"detail": "invalid or missing credentials"}):
            mismatches.append(f"{spec.label}, {case.what}: the refusal leaks detail {body}")
