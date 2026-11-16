"""T-15: per-tool approval expiry at runtime (W4.2, D-8, OD-7).

TX3 sets ``expires_at`` from the tool's policy rule (``approval_expiry_seconds``), else from the
default ``approval_ttl_seconds``. Worker startup refuses a rule or a default above
``approval_max_ttl_seconds``. Decision-time checks are unchanged: the expiry timer (TX-EA) and the
TX5 lapse compare ``expires_at`` with ``clock_timestamp()`` exactly as before, now with the
tool's value. The values are proposed and await owner confirmation (default 3600 s, maximum
86400 s).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import yaml
from pydantic import SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import ApprovalExpiryError, BootstrapFile
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS, AGENT = "ws-ae", "agent-ae"
PERMISSIONS = ["service:read", "service:restart"]
SCENARIO = (
    "name: restart\nturns:\n"
    "  - tool_calls: [{tool: restart_service, arguments: {service: checkout}}]\n"
    "  - final: restart done\n"
)


def boot(expiry: int | None) -> BootstrapFile:
    rule: dict[str, Any] = {
        "tool": "restart_service",
        "decision": "require_approval",
        "approver_roles": ["approver"],
    }
    if expiry is not None:
        rule["approval_expiry_seconds"] = expiry
    return BootstrapFile.model_validate(
        {
            "workspaces": [
                {
                    "id": WS,
                    "agents": [{"id": AGENT, "workload": WORKLOAD.name}],
                    "policy": {
                        "rules": [{"tool": "get_service_health", "decision": "allow"}, rule],
                        "role_permissions": {"operator": PERMISSIONS},
                        "agent_permissions": {AGENT: PERMISSIONS},
                    },
                }
            ]
        }
    )


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    yield
    demo_client.set_transport(None)


def settings(seeded: Any, tmp_path: Path, config: BootstrapFile, **extra: Any) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(seeded.db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
        **extra,
    )


async def open_worker(
    seeded: Any, tmp_path: Path, config: BootstrapFile, **extra: Any
) -> WorkerRuntime:
    apply_bootstrap(seeded.db.migrator_dsn, config)
    (tmp_path / "restart.yaml").write_text(SCENARIO, encoding="utf-8")
    return await WorkerRuntime.open(
        settings(seeded, tmp_path, config, **extra),
        bootstrap=config,
        provider=ScriptedProvider(tmp_path, env="test"),
    )


async def call_api(
    seeded: Any, tmp_path: Path, config: BootstrapFile, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    app = create_app(settings(seeded, tmp_path, config))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def submit(seeded: Any, tmp_path: Path, config: BootstrapFile) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": AGENT,
            "requester": {"sub": "alice", "roles": ["operator"]},
        },
        "task_input": {"message": "restart #scenario=restart"},
    }
    response = await call_api(seeded, tmp_path, config, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def approve(seeded: Any, tmp_path: Path, config: BootstrapFile, approval_id: Any) -> None:
    body = {"dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]}}
    url = f"/v1/approvals/{approval_id}/approve"
    response = await call_api(seeded, tmp_path, config, "POST", url, json=body)
    assert response.status_code == 200, response.text


async def drain(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> None:
    for _ in range(80):
        if not await process_next_job(runtime, kinds):
            return
    raise AssertionError("the queue did not drain")


def sql(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def approval(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    """(id, status, seconds from creation to expiry, seconds left now)."""
    [found] = sql(
        seeded,
        "SELECT id, status, extract(epoch FROM expires_at - created_at)::float,"
        " extract(epoch FROM expires_at - clock_timestamp())::float"
        " FROM tether.approvals WHERE run_id = %s",
        run_id,
    )
    return found


async def until_lapsed(seeded: Any, run_id: uuid.UUID) -> None:
    """Wait (by database time) until the approval's expiry has passed."""
    for _ in range(100):
        if approval(seeded, run_id)[3] < 0:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the approval did not lapse")


def run_status(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return sql(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


@pytest.mark.parametrize(
    ("expiry", "default", "expected"),
    [(120, 3600, 120), (None, 900, 900), (86400, 3600, 86400)],
    ids=["tool-value", "default", "tool-value-at-maximum"],
)
@pytest.mark.asyncio
async def test_t15_tx3_uses_the_tools_expiry_else_the_default(
    seeded: Any, tmp_path: Path, expiry: int | None, default: int, expected: int
) -> None:
    config = boot(expiry)
    runtime = await open_worker(seeded, tmp_path, config, approval_ttl_seconds=default)
    try:
        run_id = await submit(seeded, tmp_path, config)
        await drain(runtime)
        _, status, lifetime, _ = approval(seeded, run_id)
        assert status == "pending"
        # ``created_at`` is stamped by the insert, a few microseconds after TX3 computes the
        # expiry from ``clock_timestamp()``: the difference is the value to within a second.
        assert abs(lifetime - expected) < 1
        # TX3's timer is due no earlier than the stored expiry (TX-EA, unchanged).
        [(due,)] = sql(
            seeded,
            "SELECT j.run_after >= a.expires_at FROM tether.jobs j JOIN tether.approvals a"
            " ON a.id = j.ref_id WHERE j.run_id = %s AND j.kind = 'expire_approval'",
            run_id,
        )
        assert due is True
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    ("expiry", "extra"),
    [
        (86401, {}),
        (120, {"approval_max_ttl_seconds": 119}),
        (None, {"approval_ttl_seconds": 200, "approval_max_ttl_seconds": 100}),
    ],
    ids=["rule-above-default-maximum", "rule-above-configured-maximum", "default-above-maximum"],
)
@pytest.mark.asyncio
async def test_t15_worker_startup_refuses_an_expiry_above_the_maximum(
    seeded: Any, tmp_path: Path, expiry: int | None, extra: dict[str, Any]
) -> None:
    config = boot(expiry)
    with pytest.raises(ApprovalExpiryError, match="above the maximum"):
        await WorkerRuntime.open(
            settings(seeded, tmp_path, config, **extra),
            bootstrap=config,
            provider=ScriptedProvider(tmp_path, env="test"),
        )


@pytest.mark.asyncio
async def test_t15_the_active_policy_version_changes_with_the_value(
    seeded: Any, tmp_path: Path
) -> None:
    versions = []
    for expiry in (None, 120, 60):
        config = boot(expiry)
        apply_bootstrap(seeded.db.migrator_dsn, config)
        [(active,)] = sql(
            seeded,
            "SELECT version FROM tether.policy_versions WHERE workspace_id = %s AND active",
            WS,
        )
        assert active == config.workspaces[0].policy_version
        versions.append(active)
    assert len(set(versions)) == 3


@pytest.mark.asyncio
async def test_t15_the_expiry_timer_expires_a_short_per_tool_approval(
    seeded: Any, tmp_path: Path
) -> None:
    config = boot(1)
    runtime = await open_worker(seeded, tmp_path, config)
    try:
        run_id = await submit(seeded, tmp_path, config)
        await drain(runtime)
        assert approval(seeded, run_id)[1] == "pending"
        await until_lapsed(seeded, run_id)
        await drain(runtime)
        assert approval(seeded, run_id)[1] == "expired"
        events = [
            e
            for (e,) in sql(
                seeded,
                "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq",
                run_id,
            )
        ]
        assert "APPROVAL_EXPIRED" in events
        assert "EXECUTION_AUTHORIZED" not in events
        assert run_status(seeded, run_id) == ("succeeded", "restart done")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_t15_tx5_refuses_an_approval_that_lapsed_under_a_short_per_tool_value(
    seeded: Any, tmp_path: Path
) -> None:
    """Approved in time (TX4), but the worker reaches TX5 only after the tool's expiry: the
    approval no longer authorizes anything (ADR-0011 rule 6) and no ledger row is created."""
    config = boot(2)
    runtime = await open_worker(seeded, tmp_path, config)
    try:
        run_id = await submit(seeded, tmp_path, config)
        await drain(runtime)
        approval_id, status, _, left = approval(seeded, run_id)
        assert (status, left > 0) == ("pending", True)
        await approve(seeded, tmp_path, config, approval_id)
        await until_lapsed(seeded, run_id)
        await drain(runtime)
        assert approval(seeded, run_id)[1] == "approved"
        assert sql(seeded, "SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id) == []
        assert run_status(seeded, run_id) == ("succeeded", "restart done")
    finally:
        await runtime.close()
