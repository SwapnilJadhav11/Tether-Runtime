"""MVP M5: the governed run graph (allow, deny, invalid, final answer, decision adoption, policy).

Covers §9.2 call_model → govern_call (TX2) → authorize_call (TX5) → await_tool, ADR-0009 policy
version checks and replay adoption of persisted decisions (ADR-0015).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
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
from tether.core.ids import WorkspaceId
from tether.core.statuses import JobKind
from tether.policy.config import BootstrapFile
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.storage.repos.audit import AuditRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS = "ws-g"
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": "agent-g", "workload": "incident_remediation"}],
                "policy": {
                    "rules": [
                        {
                            "tool": "get_service_health",
                            "decision": "allow",
                            "requester_roles": ["operator"],
                        },
                        {
                            "tool": "restart_service",
                            "decision": "require_approval",
                            "approver_roles": ["oncall"],
                        },
                    ]
                },
            }
        ]
    }
)

SCENARIOS = {
    "answer": "name: answer\nturns:\n  - final: all good\n",
    "read": (
        "name: read\nturns:\n"
        "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
        "  - final: checked\n"
    ),
    "invalid": (
        "name: invalid\nturns:\n"
        "  - tool_calls: [{tool: get_service_health, arguments: {wrong: 1}}]\n"
        "  - final: gave up\n"
    ),
    "unknown_tool": (
        "name: unknown_tool\nturns:\n"
        "  - tool_calls: [{tool: drop_database, arguments: {}}]\n"
        "  - final: gave up\n"
    ),
}


def _settings(db: Any, tmp_path: Path) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
        worker_poll_interval_seconds=0.05,
    )


@pytest_asyncio.fixture
async def runtime(seeded: Any, tmp_path: Path) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    settings = _settings(seeded.db, tmp_path)
    runtime = await WorkerRuntime.open(
        settings, bootstrap=BOOT, provider=ScriptedProvider(tmp_path, env="test")
    )
    try:
        yield runtime
    finally:
        await runtime.close()


Submit = Callable[..., Any]


@pytest.fixture
def submit(seeded: Any, tmp_path: Path) -> Submit:
    async def _submit(scenario: str, roles: tuple[str, ...] = ("operator",)) -> uuid.UUID:
        app = create_app(_settings(seeded.db, tmp_path))
        body = {
            "dev_identity": {
                "workspace_id": WS,
                "agent_id": "agent-g",
                "requester": {"sub": "alice", "roles": list(roles)},
            },
            "task_input": {"message": f"fix checkout #scenario={scenario}"},
        }
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
        ):
            response = await http.post("/v1/tasks/runs", json=body)
        assert response.status_code == 201, response.text
        return uuid.UUID(response.json()["run_id"])

    return _submit


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    yield
    demo_client.set_transport(None)


async def drain(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> None:
    for _ in range(50):
        if not await process_next_job(runtime, kinds=kinds):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_status(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return rows(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event_type for (event_type,) in found]


@pytest.mark.asyncio
async def test_a_final_answer_completes_the_run(
    runtime: WorkerRuntime, submit: Submit, seeded: Any
) -> None:
    run_id = await submit("answer")
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "all good")
    assert events(seeded, run_id) == ["RUN_SUBMITTED", "RUN_FINISHED"]
    assert rows(seeded, "SELECT status FROM tether.model_calls WHERE run_id = %s", run_id) == [
        ("succeeded",)
    ]


@pytest.mark.asyncio
async def test_an_allowed_call_is_authorized_into_the_ledger_and_waits_for_the_tool(
    runtime: WorkerRuntime, submit: Submit, seeded: Any
) -> None:
    run_id = await submit("read")
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    assert run_status(seeded, run_id)[0] == "awaiting_tool"
    ledger = rows(
        seeded,
        "SELECT id, step_ordinal, tool, state, effect, call_hash, idempotency_key"
        " FROM tether.ledger_entries WHERE run_id = %s",
        run_id,
    )
    assert len(ledger) == 1
    entry_id, ordinal, tool, state, effect, call_hash, key = ledger[0]
    assert (ordinal, tool, state, effect) == (0, "get_service_health", "pending", "read")
    assert key.startswith("tth_")
    assert rows(
        seeded,
        "SELECT kind, state, ref_id FROM tether.jobs WHERE run_id = %s AND kind = 'execute_tool'",
        run_id,
    ) == [("execute_tool", "ready", entry_id)]
    assert events(seeded, run_id) == ["RUN_SUBMITTED", "POLICY_DECISION", "EXECUTION_AUTHORIZED"]
    decision = rows(
        seeded,
        "SELECT payload_masked FROM tether.audit_events"
        " WHERE run_id = %s AND event_type = 'POLICY_DECISION'",
        run_id,
    )[0][0]
    assert decision["decision"] == "allow"
    assert decision["call_hash"] == call_hash


@pytest.mark.asyncio
async def test_a_denied_call_never_reaches_the_ledger(
    runtime: WorkerRuntime, submit: Submit, seeded: Any
) -> None:
    run_id = await submit("read", roles=())  # no matching rule for this requester: deny
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "checked")
    assert rows(seeded, "SELECT count(*) FROM tether.ledger_entries") == [(0,)]
    assert events(seeded, run_id) == ["RUN_SUBMITTED", "POLICY_DECISION", "RUN_FINISHED"]


@pytest.mark.parametrize("scenario", ["invalid", "unknown_tool"])
@pytest.mark.asyncio
async def test_invalid_proposals_are_recorded_and_not_executed(
    runtime: WorkerRuntime, submit: Submit, seeded: Any, scenario: str
) -> None:
    run_id = await submit(scenario)
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "gave up")
    assert rows(seeded, "SELECT count(*) FROM tether.ledger_entries") == [(0,)]
    assert events(seeded, run_id) == ["RUN_SUBMITTED", "CALL_INVALID", "RUN_FINISHED"]


@pytest.mark.asyncio
async def test_a_persisted_decision_is_adopted_not_re_decided(
    runtime: WorkerRuntime, submit: Submit, seeded: Any
) -> None:
    """Replay safety: an earlier invocation already recorded 'deny' for step 0."""
    run_id = await submit("read")
    async with UnitOfWork(runtime.engine) as uow:
        await uow.repository(AuditRepository, WorkspaceScope(WorkspaceId(WS))).append(
            runtime.keys,
            run_id=run_id,
            dedupe_key="decision:0",
            event_type="POLICY_DECISION",
            payload={"decision": "deny", "reasons": ["recorded earlier"], "call_hash": None},
        )
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "checked")
    assert rows(seeded, "SELECT count(*) FROM tether.ledger_entries") == [(0,)]


@pytest.mark.asyncio
async def test_no_decision_is_made_under_a_stale_policy_version(
    runtime: WorkerRuntime, submit: Submit, seeded: Any
) -> None:
    run_id = await submit("read")
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        conn.execute(
            "UPDATE tether.policy_versions SET active = false WHERE workspace_id = %s", (WS,)
        )
        conn.execute(
            "INSERT INTO tether.policy_versions (workspace_id, version, active)"
            " VALUES (%s, 'someone-else', true)",
            (WS,),
        )
    assert await process_next_job(runtime) is True
    assert run_status(seeded, run_id)[0] == "running"
    assert events(seeded, run_id) == ["RUN_SUBMITTED"]
    # Re-queued for later without consuming an attempt.
    assert rows(
        seeded,
        "SELECT state, attempts FROM tether.jobs WHERE run_id = %s AND kind = 'advance_run'",
        run_id,
    ) == [("ready", 0)]


# --- M6: execute_tool (TX6 dispatch commit, TX6r re-dispatch, TX7 outcome) -----------------

UNKNOWN_SERVICE = (
    "name: unknown_service\nturns:\n"
    "  - tool_calls: [{tool: get_service_health, arguments: {service: nope}}]\n"
    "  - final: not found\n"
)


def ledger(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return rows(
        seeded,
        "SELECT state, attempts, outcome_payload_masked, outcome_reason"
        " FROM tether.ledger_entries WHERE run_id = %s",
        run_id,
    )


@pytest.mark.asyncio
async def test_an_allowed_read_executes_and_the_run_completes(
    runtime: WorkerRuntime, submit: Submit, seeded: Any
) -> None:
    run_id = await submit("read")
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "checked")
    assert ledger(seeded, run_id) == [
        ("completed", 1, {"service": "checkout", "healthy": False, "restarts": 0}, None)
    ]
    assert events(seeded, run_id) == [
        "RUN_SUBMITTED",
        "POLICY_DECISION",
        "EXECUTION_AUTHORIZED",
        "DISPATCHED",
        "TOOL_OUTCOME",
        "RUN_FINISHED",
    ]


@pytest.mark.asyncio
async def test_a_tool_that_did_not_apply_is_recorded_as_failed(
    runtime: WorkerRuntime, submit: Submit, seeded: Any, tmp_path: Path
) -> None:
    (tmp_path / "unknown_service.yaml").write_text(UNKNOWN_SERVICE, encoding="utf-8")
    run_id = await submit("unknown_service")
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "not found")
    assert ledger(seeded, run_id) == [("failed", 1, None, "not applied: unknown service")]


@pytest.mark.asyncio
async def test_a_tampered_job_payload_is_never_dispatched(
    runtime: WorkerRuntime, submit: Submit, seeded: Any
) -> None:
    run_id = await submit("read")
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    changed = rows(
        seeded,
        'UPDATE tether.jobs SET payload = \'{"arguments": {"service": "other"}}\''
        " WHERE run_id = %s AND kind = 'execute_tool' RETURNING id",
        run_id,
    )
    assert len(changed) == 1
    await drain(runtime)
    assert ledger(seeded, run_id) == [("failed", 0, None, "call hash mismatch: not dispatched")]
    assert "DISPATCHED" not in events(seeded, run_id)


@pytest.mark.asyncio
async def test_a_redelivered_dispatched_idempotent_call_is_re_dispatched(
    runtime: WorkerRuntime, submit: Submit, seeded: Any
) -> None:
    """Crash after TX6: the entry is dispatched but no outcome was recorded."""
    run_id = await submit("read")
    await drain(runtime, kinds=(JobKind.ADVANCE_RUN,))
    rows(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'dispatched', attempts = 1"
        " WHERE run_id = %s RETURNING id",
        run_id,
    )
    await drain(runtime)
    assert ledger(seeded, run_id)[0][:2] == ("completed", 2)
    assert "REDISPATCHED" in events(seeded, run_id)
    assert run_status(seeded, run_id) == ("succeeded", "checked")


# --- M7: approvals (TX3 request, TX4 decision, TX5 mapping) --------------------------------

RESTART = (
    "name: restart\nturns:\n"
    "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
    "  - tool_calls: [{tool: restart_service, arguments: {service: checkout}}]\n"
    "  - final: restarted\n"
)


async def decide(
    seeded: Any,
    tmp_path: Path,
    approval_id: Any,
    action: str,
    sub: str,
    roles: list[str],
    workspace: str = WS,
    env: str = "test",
) -> httpx.Response:
    settings = _settings(seeded.db, tmp_path).model_copy(update={"env": env})
    app = create_app(settings)
    body = {"dev_identity": {"workspace_id": workspace, "sub": sub, "roles": roles}, "reason": "ok"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.post(f"/v1/approvals/{approval_id}/{action}", json=body)


async def awaiting_approval(runtime: WorkerRuntime, submit: Submit, tmp_path: Path) -> uuid.UUID:
    (tmp_path / "restart.yaml").write_text(RESTART, encoding="utf-8")
    run_id = await submit("restart")
    await drain(runtime)
    return run_id


def approval(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return rows(
        seeded,
        "SELECT id, status, tool, call_hash, arguments_masked, decided_by"
        " FROM tether.approvals WHERE run_id = %s",
        run_id,
    )[0]


WRITES = "SELECT count(*) FROM tether.ledger_entries WHERE run_id = %s AND effect = 'write'"


@pytest.mark.asyncio
async def test_a_call_requiring_approval_pauses_the_run_with_a_bound_request(
    runtime: WorkerRuntime, submit: Submit, seeded: Any, tmp_path: Path
) -> None:
    run_id = await awaiting_approval(runtime, submit, tmp_path)
    assert run_status(seeded, run_id)[0] == "awaiting_approval"
    _, status, tool, call_hash, arguments, decided_by = approval(seeded, run_id)
    assert (status, tool, arguments, decided_by) == (
        "pending",
        "restart_service",
        {"service": "checkout"},
        None,
    )
    decision = rows(
        seeded,
        "SELECT payload_masked FROM tether.audit_events WHERE run_id = %s"
        " AND event_type = 'POLICY_DECISION' AND tool = 'restart_service'",
        run_id,
    )[0][0]
    assert decision["call_hash"] == call_hash
    assert "APPROVAL_REQUESTED" in events(seeded, run_id)
    assert rows(seeded, WRITES, run_id) == [(0,)]


@pytest.mark.asyncio
async def test_an_eligible_approval_executes_the_write_and_completes_the_run(
    runtime: WorkerRuntime, submit: Submit, seeded: Any, tmp_path: Path
) -> None:
    run_id = await awaiting_approval(runtime, submit, tmp_path)
    approval_id = approval(seeded, run_id)[0]
    response = await decide(seeded, tmp_path, approval_id, "approve", "bob", ["oncall"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "restarted")
    write = rows(
        seeded,
        "SELECT state, outcome_payload_masked FROM tether.ledger_entries"
        " WHERE run_id = %s AND effect = 'write'",
        run_id,
    )
    assert write == [("completed", {"service": "checkout", "healthy": True, "restarts": 1})]
    decided = rows(
        seeded,
        "SELECT actor, policy_version FROM tether.audit_events"
        " WHERE run_id = %s AND event_type = 'APPROVAL_DECIDED'",
        run_id,
    )
    assert decided == [("bob", BOOT.workspaces[0].policy_version)]


@pytest.mark.parametrize(
    ("sub", "roles", "workspace"),
    [
        ("alice", ["oncall"], WS),  # the requester cannot approve their own call
        ("carol", ["viewer"], WS),  # no approver role
        ("bob", ["oncall"], "ws-a"),  # another workspace: not found
    ],
)
@pytest.mark.asyncio
async def test_ineligible_approvers_get_not_found_and_change_nothing(
    runtime: WorkerRuntime,
    submit: Submit,
    seeded: Any,
    tmp_path: Path,
    sub: str,
    roles: list[str],
    workspace: str,
) -> None:
    run_id = await awaiting_approval(runtime, submit, tmp_path)
    approval_id = approval(seeded, run_id)[0]
    response = await decide(seeded, tmp_path, approval_id, "approve", sub, roles, workspace)
    assert response.status_code == 404
    assert approval(seeded, run_id)[1] == "pending"
    assert "APPROVAL_DECIDED" not in events(seeded, run_id)


@pytest.mark.asyncio
async def test_a_rejected_call_is_never_executed(
    runtime: WorkerRuntime, submit: Submit, seeded: Any, tmp_path: Path
) -> None:
    run_id = await awaiting_approval(runtime, submit, tmp_path)
    approval_id = approval(seeded, run_id)[0]
    response = await decide(seeded, tmp_path, approval_id, "reject", "bob", ["oncall"])
    assert response.status_code == 200
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "succeeded"
    assert rows(seeded, WRITES, run_id) == [(0,)]


@pytest.mark.asyncio
async def test_an_approval_is_decided_once_and_only_before_it_expires(
    runtime: WorkerRuntime, submit: Submit, seeded: Any, tmp_path: Path
) -> None:
    run_id = await awaiting_approval(runtime, submit, tmp_path)
    approval_id = approval(seeded, run_id)[0]
    first = await decide(seeded, tmp_path, approval_id, "reject", "bob", ["oncall"])
    assert first.status_code == 200
    again = await decide(seeded, tmp_path, approval_id, "approve", "bob", ["oncall"])
    assert again.status_code == 409

    other = await awaiting_approval(runtime, submit, tmp_path)
    other_id = approval(seeded, other)[0]
    rows(
        seeded,
        "UPDATE tether.approvals SET expires_at = clock_timestamp() - interval '1 s'"
        " WHERE id = %s RETURNING id",
        other_id,
    )
    late = await decide(seeded, tmp_path, other_id, "approve", "bob", ["oncall"])
    assert late.status_code == 409
    assert approval(seeded, other)[1] == "pending"


@pytest.mark.asyncio
async def test_the_dev_approver_shortcut_is_refused_in_prod(
    runtime: WorkerRuntime, submit: Submit, seeded: Any, tmp_path: Path
) -> None:
    run_id = await awaiting_approval(runtime, submit, tmp_path)
    approval_id = approval(seeded, run_id)[0]
    response = await decide(seeded, tmp_path, approval_id, "approve", "bob", ["oncall"], env="prod")
    assert response.status_code == 404
    assert approval(seeded, run_id)[1] == "pending"


# --- M8: run inspection ------------------------------------------------------------------


async def inspect_run(seeded: Any, tmp_path: Path, run_id: uuid.UUID, workspace: str = WS) -> Any:
    app = create_app(_settings(seeded.db, tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.get(f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": workspace})


@pytest.mark.asyncio
async def test_a_governed_run_can_be_inspected_end_to_end(
    runtime: WorkerRuntime, submit: Submit, seeded: Any, tmp_path: Path
) -> None:
    run_id = await awaiting_approval(runtime, submit, tmp_path)
    paused = (await inspect_run(seeded, tmp_path, run_id)).json()
    assert paused["status"] == "awaiting_approval"
    [pending] = paused["approvals"]
    assert (pending["status"], pending["tool"], pending["arguments_masked"]) == (
        "pending",
        "restart_service",
        {"service": "checkout"},
    )
    await decide(seeded, tmp_path, pending["id"], "approve", "bob", ["oncall"])
    await drain(runtime)

    response = await inspect_run(seeded, tmp_path, run_id)
    assert response.status_code == 200
    view = response.json()
    assert (view["status"], view["outcome_summary"]) == ("succeeded", "restarted")
    assert [(a["status"], a["decided_by"]) for a in view["approvals"]] == [("approved", "bob")]
    assert [(e["step_ordinal"], e["tool"], e["state"]) for e in view["ledger"]] == [
        (0, "get_service_health", "completed"),
        (1, "restart_service", "completed"),
    ]
    assert view["ledger"][1]["outcome"] == {"service": "checkout", "healthy": True, "restarts": 1}
    timeline = [event["event_type"] for event in view["audit"]]
    assert timeline[0] == "RUN_SUBMITTED"
    assert timeline[-1] == "RUN_FINISHED"
    assert "APPROVAL_DECIDED" in timeline
    assert view["audit_chain_valid"] is True

    other = await inspect_run(seeded, tmp_path, run_id, workspace="ws-a")
    assert other.status_code == 404
