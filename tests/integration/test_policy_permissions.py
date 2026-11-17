"""W3.1, T-06 (integration half): the policy engine at TX2, TX5 and the approval route.

ADR-0009 and ADR-0020 on the real reference workload: requester permissions (from snapshotted
roles) ∩ agent permissions must cover the tool's required permissions; the decision is made at
TX2 and again, under current policy, at TX5; a stale process decides nothing; a worker refuses to
start with a policy that allows a high-risk tool.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from pydantic import SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.core.statuses import JobKind
from tether.policy.config import BootstrapConfigError, BootstrapFile
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS = "ws-p"
POLICY: dict[str, Any] = {
    "role_permissions": {
        "operator": ["service:read", "service:restart"],
        "viewer": ["service:read"],
    },
    "agent_permissions": {"agent-p": ["service:read", "service:restart"]},
    "rules": [
        {"tool": "get_service_health", "decision": "allow"},
        {
            "tool": "restart_service",
            "decision": "require_approval",
            "approver_roles": ["approver"],
        },
    ],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
}
SCENARIOS = {
    "restart": (
        "name: restart\nturns:\n"
        "  - tool_calls: [{tool: restart_service, arguments: {service: checkout}}]\n"
        "  - final: restart done\n"
    ),
    "read": (
        "name: read\nturns:\n"
        "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
        "  - final: read done\n"
    ),
}


def boot(policy: dict[str, Any]) -> BootstrapFile:
    return BootstrapFile.model_validate(
        {
            "workspaces": [
                {
                    "id": WS,
                    "agents": [{"id": "agent-p", "workload": "incident_remediation"}],
                    "policy": policy,
                }
            ]
        }
    )


def revoked(policy: dict[str, Any], *, agent: bool) -> dict[str, Any]:
    """The same policy without ``service:restart`` for the agent, or for the operator role."""
    changed = copy.deepcopy(policy)
    if agent:
        changed["agent_permissions"]["agent-p"] = ["service:read"]
    else:
        changed["role_permissions"]["operator"] = ["service:read"]
    return changed


BOOT = boot(POLICY)


def _settings(db: Any, tmp_path: Path, config: BootstrapFile) -> Settings:
    bootstrap_file = tmp_path / f"bootstrap-{uuid.uuid4().hex[:8]}.yaml"
    bootstrap_file.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    yield
    demo_client.set_transport(None)


@pytest.fixture
def scenarios(tmp_path: Path) -> Path:
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    return tmp_path


async def open_worker(seeded: Any, tmp_path: Path, config: BootstrapFile) -> WorkerRuntime:
    return await WorkerRuntime.open(
        _settings(seeded.db, tmp_path, config),
        bootstrap=config,
        provider=ScriptedProvider(tmp_path, env="test"),
    )


@pytest_asyncio.fixture
async def worker(seeded: Any, scenarios: Path) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    runtime = await open_worker(seeded, scenarios, BOOT)
    try:
        yield runtime
    finally:
        await runtime.close()


async def call(
    seeded: Any, tmp_path: Path, method: str, url: str, config: BootstrapFile = BOOT, **kw: Any
) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path, config))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kw)


async def submit(
    seeded: Any,
    tmp_path: Path,
    scenario: str,
    roles: Sequence[str] = ("operator",),
    config: BootstrapFile = BOOT,
) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": "agent-p",
            "requester": {"sub": "alice", "roles": list(roles)},
        },
        "task_input": {"message": f"go #scenario={scenario}"},
    }
    response = await call(seeded, tmp_path, "POST", "/v1/tasks/runs", config, json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def approve(
    seeded: Any, tmp_path: Path, approval_id: Any, config: BootstrapFile = BOOT
) -> httpx.Response:
    body = {
        "dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]},
        "reason": "ok",
    }
    return await call(
        seeded, tmp_path, "POST", f"/v1/approvals/{approval_id}/approve", config, json=body
    )


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(60):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def decisions(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    """(tool, decision, reasons, policy_version) of every POLICY_DECISION, in order."""
    found = rows(
        seeded,
        "SELECT tool, payload_masked->>'decision', payload_masked->'reasons', policy_version"
        " FROM tether.audit_events WHERE run_id = %s AND event_type = 'POLICY_DECISION'"
        " ORDER BY seq",
        run_id,
    )
    return [tuple(row) for row in found]


def ledger_tools(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(seeded, "SELECT tool FROM tether.ledger_entries WHERE run_id = %s", run_id)
    return [tool for (tool,) in found]


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event for (event,) in found]


def status(seeded: Any, run_id: uuid.UUID) -> str:
    return str(rows(seeded, "SELECT status FROM tether.runs WHERE id = %s", run_id)[0][0])


def approval_id(seeded: Any, run_id: uuid.UUID) -> Any:
    [(found,)] = rows(seeded, "SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
    return found


# --- TX2 -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t06_a_permitted_read_is_allowed_with_its_reason_and_policy_version(
    worker: WorkerRuntime, seeded: Any, scenarios: Path
) -> None:
    run_id = await submit(seeded, scenarios, "read")
    await drain(worker)
    assert status(seeded, run_id) == "succeeded"
    assert decisions(seeded, run_id) == [
        ("get_service_health", "allow", ["rule_allow"], BOOT.workspaces[0].policy_version)
    ]
    assert ledger_tools(seeded, run_id) == ["get_service_health"]


@pytest.mark.asyncio
async def test_t06_a_requester_without_the_permission_is_denied_at_tx2(
    worker: WorkerRuntime, seeded: Any, scenarios: Path
) -> None:
    run_id = await submit(seeded, scenarios, "restart", roles=("viewer",))
    await drain(worker)
    assert status(seeded, run_id) == "succeeded"  # the model got a typed denial and finished
    [(tool, decision, reasons, _)] = decisions(seeded, run_id)
    assert (tool, decision, reasons) == (
        "restart_service",
        "deny",
        ["requester_lacks:service:restart"],
    )
    approvals = rows(seeded, "SELECT count(*) FROM tether.approvals WHERE run_id = %s", run_id)
    assert approvals == [(0,)]
    assert ledger_tools(seeded, run_id) == []


@pytest.mark.asyncio
async def test_t06_an_agent_without_the_permission_is_denied_even_for_a_permitted_requester(
    seeded: Any, scenarios: Path
) -> None:
    config = boot(revoked(POLICY, agent=True))
    apply_bootstrap(seeded.db.migrator_dsn, config)
    runtime = await open_worker(seeded, scenarios, config)
    try:
        run_id = await submit(seeded, scenarios, "restart", config=config)
        await drain(runtime)
        [(_, decision, reasons, _)] = decisions(seeded, run_id)
        assert (decision, reasons) == ("deny", ["agent_lacks:service:restart"])
        assert ledger_tools(seeded, run_id) == []
    finally:
        await runtime.close()


# --- TX5: current policy after approval -----------------------------------------------------


@pytest.mark.parametrize("agent", [True, False], ids=["agent-revoked", "role-revoked"])
@pytest.mark.asyncio
async def test_t06_t19_a_permission_revoked_after_approval_is_refused_at_tx5(
    worker: WorkerRuntime, seeded: Any, scenarios: Path, agent: bool
) -> None:
    run_id = await submit(seeded, scenarios, "restart")
    await drain(worker)
    assert status(seeded, run_id) == "awaiting_approval"
    assert (await approve(seeded, scenarios, approval_id(seeded, run_id))).status_code == 200
    # A policy change is a deployment (ADR-0009 rule 7): apply, then a worker with the new file.
    tightened = boot(revoked(POLICY, agent=agent))
    apply_bootstrap(seeded.db.migrator_dsn, tightened)
    current = await open_worker(seeded, scenarios, tightened)
    try:
        await drain(current)
    finally:
        await current.close()
    assert ledger_tools(seeded, run_id) == []  # no ledger row: nothing was authorized or sent
    assert "EXECUTION_AUTHORIZED" not in events(seeded, run_id)
    assert status(seeded, run_id) == "succeeded"


@pytest.mark.asyncio
async def test_g15_a_stale_worker_decides_nothing_at_tx5(
    worker: WorkerRuntime, seeded: Any, scenarios: Path
) -> None:
    run_id = await submit(seeded, scenarios, "restart")
    await drain(worker)
    assert (await approve(seeded, scenarios, approval_id(seeded, run_id))).status_code == 200
    apply_bootstrap(seeded.db.migrator_dsn, boot(revoked(POLICY, agent=True)))
    # The old worker still holds the previous version: TX5 records nothing and requeues.
    assert await process_next_job(worker, kinds=(JobKind.ADVANCE_RUN,))
    assert ledger_tools(seeded, run_id) == []
    assert "EXECUTION_AUTHORIZED" not in events(seeded, run_id)
    assert rows(
        seeded,
        "SELECT state, attempts FROM tether.jobs WHERE run_id = %s AND kind = 'advance_run'"
        " AND state <> 'done'",
        run_id,
    ) == [("ready", 0)]


# --- the approval route ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g15_the_approval_route_answers_503_under_a_stale_policy_and_changes_nothing(
    worker: WorkerRuntime, seeded: Any, scenarios: Path
) -> None:
    run_id = await submit(seeded, scenarios, "restart")
    await drain(worker)
    target = approval_id(seeded, run_id)
    apply_bootstrap(seeded.db.migrator_dsn, boot(revoked(POLICY, agent=True)))
    response = await approve(seeded, scenarios, target)  # the api still loads the old file
    assert response.status_code == 503
    assert rows(seeded, "SELECT status FROM tether.approvals WHERE id = %s", target) == [
        ("pending",)
    ]
    assert "APPROVAL_DECIDED" not in events(seeded, run_id)


@pytest.mark.asyncio
async def test_t19_the_requester_cannot_approve_their_own_call(
    worker: WorkerRuntime, seeded: Any, scenarios: Path
) -> None:
    run_id = await submit(seeded, scenarios, "restart")
    await drain(worker)
    target = approval_id(seeded, run_id)
    body = {
        "dev_identity": {"workspace_id": WS, "sub": "alice", "roles": ["approver"]},
        "reason": "mine",
    }
    response = await call(seeded, scenarios, "POST", f"/v1/approvals/{target}/approve", json=body)
    assert response.status_code == 404
    assert rows(seeded, "SELECT status FROM tether.approvals WHERE id = %s", target) == [
        ("pending",)
    ]


# --- startup --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_worker_refuses_to_start_with_a_policy_that_allows_a_high_risk_tool(
    seeded: Any, scenarios: Path
) -> None:
    unsafe = copy.deepcopy(POLICY)
    unsafe["rules"][1] = {"tool": "restart_service", "decision": "allow"}
    with pytest.raises(BootstrapConfigError, match="restart_service"):
        await open_worker(seeded, scenarios, boot(unsafe))
