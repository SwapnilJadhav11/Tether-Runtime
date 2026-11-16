"""W4.3 / G1: ``GET /v1/ops/runs/{id}``, the auditor and workspace-admin run view.

PRD authorization table: audience ``tether-ops``, roles ``auditor`` and ``workspace_admin``, every
run in their own workspace. Anything else is ``404`` (another workspace, a missing role, an unknown
run); a bad or wrongly-audienced credential is ``401`` before the handler; the dev/test shortcut
never applies to this read. The content is the masked inspection the requester gets (ADR-0017).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from tests.authz.fixtures import (
    APPROVALS,
    FIXTURE_B,
    OPS,
    OPS_DEMO,
    RAW_VALUE,
    STRANGER,
    TASKS,
    World,
    headers,
    person,
    token,
)

AUDITOR = ("audrey", ["auditor"])
ADMIN = ("adam", ["workspace_admin"])


def ops(ws: str, sub: str, roles: list[str]) -> dict[str, str]:
    return person(ws, OPS, sub, roles)


def view_url(run_id: uuid.UUID | str) -> str:
    return f"/v1/ops/runs/{run_id}"


@pytest.mark.parametrize("viewer", [AUDITOR, ADMIN], ids=["auditor", "workspace_admin"])
@pytest.mark.asyncio
async def test_t24_g1_an_auditor_or_workspace_admin_inspects_any_run_of_their_workspace(
    world: World, viewer: tuple[str, list[str]]
) -> None:
    run_id = await world.submit(OPS_DEMO, "restart", sub="alice")
    response = await world.request(
        "GET", view_url(run_id), headers=ops(OPS_DEMO, viewer[0], viewer[1])
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(run_id)
    assert body["status"] == "awaiting_approval"
    assert body["agent_id"] == "agent-1"
    assert body["audit_chain_valid"] is True
    [approval] = body["approvals"]
    assert approval["tool"] == "restart_service"
    assert approval["status"] == "pending"
    assert body["audit"], "the audit timeline is part of the view"
    assert [event["seq"] for event in body["audit"]] == sorted(
        event["seq"] for event in body["audit"]
    )
    assert body["audit"][0]["event_type"] == "RUN_SUBMITTED"


@pytest.mark.asyncio
async def test_t24_g1_the_view_carries_the_ledger_and_masks_sensitive_arguments(
    world: World,
) -> None:
    # A run that wrote to the ledger (an indeterminate write) and one whose approval holds a
    # ``Sensitive`` argument.
    nonidem = await world.submit(OPS_DEMO, "nonidem")
    rotate = await world.submit(OPS_DEMO, "rotate")
    caller = ops(OPS_DEMO, *AUDITOR)
    ledger = await world.request("GET", view_url(nonidem), headers=caller)
    assert ledger.status_code == 200, ledger.text
    [entry] = ledger.json()["ledger"]
    assert entry["tool"] == "probe_nonidem"
    assert entry["state"] == "indeterminate"
    sensitive = await world.request("GET", view_url(rotate), headers=caller)
    assert sensitive.status_code == 200, sensitive.text
    assert RAW_VALUE not in sensitive.text
    [approval] = sensitive.json()["approvals"]
    assert approval["arguments_masked"]["service"] == "checkout"
    assert approval["arguments_masked"]["api_token"] != RAW_VALUE


@pytest.mark.asyncio
async def test_t24_g1_the_view_reports_a_broken_chain(world: World) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    caller = ops(OPS_DEMO, *AUDITOR)
    intact = await world.request("GET", view_url(run_id), headers=caller)
    assert intact.json()["audit_chain_valid"] is True
    # Someone with database access rewrites an event (the migrator role can; the app role cannot).
    world.rows(
        "UPDATE tether.audit_events SET actor = 'mallory' WHERE run_id = %s AND seq = 1"
        " RETURNING 1",
        run_id,
    )
    tampered = await world.request("GET", view_url(run_id), headers=caller)
    assert tampered.status_code == 200
    assert tampered.json()["audit_chain_valid"] is False


@pytest.mark.asyncio
async def test_t24_g1_another_workspace_a_missing_role_or_an_unknown_run_is_404(
    world: World,
) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    foreign_run = await world.submit(FIXTURE_B, "restart", sub="mallory")
    refused = {
        "auditor of the other workspace": (ops(FIXTURE_B, "frida", ["auditor"]), run_id),
        "admin of the other workspace": (ops(FIXTURE_B, "fred", ["workspace_admin"]), run_id),
        "the other workspace's run, from this one": (ops(OPS_DEMO, *AUDITOR), foreign_run),
        "no role": (ops(OPS_DEMO, "nobody", []), run_id),
        "reconciler only": (ops(OPS_DEMO, "rita", ["reconciler", "sre"]), run_id),
        "approver only": (ops(OPS_DEMO, "bob", ["approver", "oncall"]), run_id),
        "the requester's own roles": (ops(OPS_DEMO, "alice", ["operator"]), run_id),
        "unknown run": (ops(OPS_DEMO, *AUDITOR), uuid.uuid4()),
    }
    before = world.counts()
    for name, (caller, target) in refused.items():
        response = await world.request("GET", view_url(target), headers=caller)
        assert response.status_code == 404, (name, response.text)
        assert response.json() == {"detail": "Not Found"}, name
    assert world.counts() == before


@pytest.mark.asyncio
async def test_t24_g1_bad_credentials_are_401_before_the_handler(world: World) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    credentials: dict[str, dict[str, str]] = {
        "no credentials": headers(),
        "a tasks token": headers(token(OPS_DEMO, TASKS, "audrey", ["auditor"])),
        "an approvals token": headers(token(OPS_DEMO, APPROVALS, "audrey", ["auditor"])),
        "a key nobody trusts": headers(token(OPS_DEMO, OPS, "audrey", ["auditor"], key=STRANGER)),
        "an expired token": headers(token(OPS_DEMO, OPS, "audrey", ["auditor"], exp=1)),
        "garbage": {"Authorization": "Bearer not-a-jwt"},
        "a malformed header": {"Authorization": "Token abc"},
    }
    before = world.counts()
    for name, caller in credentials.items():
        response = await world.request("GET", view_url(run_id), headers=caller)
        assert response.status_code == 401, (name, response.text)
        assert response.headers.get("www-authenticate") == "Bearer", name
    assert world.counts() == before


@pytest.mark.parametrize("env", ["test", "dev", "prod"])
@pytest.mark.asyncio
async def test_t24_g1_no_identity_shortcut_reaches_the_ops_run_view(world: World, env: str) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    dev: dict[str, Any] = {"workspace_id": OPS_DEMO, "sub": "audrey", "roles": ["auditor"]}
    attempts = (
        {"params": {"dev_workspace_id": OPS_DEMO}},
        {"json": {"dev_identity": dev}},
    )
    for kwargs in attempts:
        response = await world.request("GET", view_url(run_id), env=env, **kwargs)
        assert response.status_code == 401, (env, kwargs, response.text)


@pytest.mark.asyncio
async def test_t24_g1_the_view_works_in_production_with_real_credentials(world: World) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    response = await world.request(
        "GET", view_url(run_id), env="prod", headers=ops(OPS_DEMO, *AUDITOR)
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_t24_g1_the_view_only_reads(world: World) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    before = world.rows(
        "SELECT (SELECT count(*) FROM tether.runs), (SELECT count(*) FROM tether.approvals),"
        " (SELECT count(*) FROM tether.audit_events), (SELECT count(*) FROM tether.jobs),"
        " (SELECT count(*) FROM tether.ledger_entries)"
    )
    response = await world.request("GET", view_url(run_id), headers=ops(OPS_DEMO, *ADMIN))
    assert response.status_code == 200
    after = world.rows(
        "SELECT (SELECT count(*) FROM tether.runs), (SELECT count(*) FROM tether.approvals),"
        " (SELECT count(*) FROM tether.audit_events), (SELECT count(*) FROM tether.jobs),"
        " (SELECT count(*) FROM tether.ledger_entries)"
    )
    assert after == before
