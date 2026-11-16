"""T-28 (W4.1): an approver lists, inspects and decides approvals with real credentials.

``GET /v1/approvals/approvals`` (pending, unexpired, eligible, run not stopped), ``.../{id}``
(any status, eligible) and ``GET /v1/approvals/runs/{run_id}`` (evidence for a run with an approval
the caller is eligible for). Out of scope or ineligible is ``404``; credential failures are ``401``;
nothing is written; only masked data leaves the API (ADR-0013, ADR-0017, ADR-0009).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import pytest
import pytest_asyncio
import yaml
from pydantic import BaseModel, SecretStr
from tests.integration.test_authentication import (
    APP_KEY,
    APPROVALS,
    BOOT,
    OPS,
    OTHER_APP_KEY,
    OTHER_WS,
    PERMISSIONS,
    SCENARIOS,
    STRANGER,
    TASKS,
    TEST_WORKLOAD,
    WS,
    demo_target,  # noqa: F401  (autouse fixture)
    headers,
    rows,
    token,
)

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, Sensitive, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

import httpx  # isort: skip

RAW_VALUE = "s3cr3t-token-value-91827"


class RotateIn(BaseModel):
    service: str
    api_token: Annotated[str, Sensitive]


class RotateOut(BaseModel):
    service: str


async def _rotate(args: RotateIn, ctx: ToolContext) -> RotateOut:
    return RotateOut(service=args.service)


ROTATE = ToolDescriptor(
    name="rotate_credentials",
    version="1.0.0",
    description="Test-only write with a sensitive argument.",
    input_model=RotateIn,
    output_model=RotateOut,
    effect="write",
    idempotency="supported",
    handler=_rotate,
    risk="high",
    required_permissions=("service:restart",),
)
WORKLOAD2 = Workload(
    name=TEST_WORKLOAD.name,
    tools=(*TEST_WORKLOAD.tools, ROTATE),
    agent=AgentDefinition(system_prompt="test", tools=(*TEST_WORKLOAD.agent.tools, ROTATE.name)),
)


def _boot() -> BootstrapFile:
    data = BOOT.model_dump(mode="json")
    for workspace in data["workspaces"]:
        workspace["policy"]["rules"].append(
            {
                "tool": "rotate_credentials",
                "decision": "require_approval",
                # ``auditor`` is a role bob lacks; only ``vault`` holders may decide it.
                "approver_roles": ["vault"],
            }
        )
    return BootstrapFile.model_validate(data)


BOOT2 = _boot()
ROTATE_SCENARIO = (
    "name: rotate\nturns:\n"
    "  - tool_calls: [{tool: rotate_credentials,"
    f" arguments: {{service: checkout, api_token: {RAW_VALUE}}}}}]\n"
    "  - final: rotated\n"
)


def _settings(db: Any, tmp_path: Path, env: str = "test") -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT2.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env=env,
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


@pytest_asyncio.fixture
async def runtime(seeded: Any, tmp_path: Path) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT2)
    for name, text in {**SCENARIOS, "rotate": ROTATE_SCENARIO}.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path),
        bootstrap=BOOT2,
        provider=ScriptedProvider(tmp_path, env="test"),
    )
    runtime.registries[WORKLOAD2.name] = ToolRegistry(WORKLOAD2)
    try:
        yield runtime
    finally:
        await runtime.close()


async def call(
    seeded: Any, tmp_path: Path, method: str, url: str, *, env: str = "test", **kwargs: Any
) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path, env=env))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(60):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


async def submit(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    scenario: str = "restart",
    *,
    requester: str = "alice",
    ws: str = WS,
) -> uuid.UUID:
    key = APP_KEY if ws == WS else OTHER_APP_KEY
    creds = headers(token(requester, ["operator"], TASKS, ws=ws), key.raw)
    response = await call(
        seeded,
        tmp_path,
        "POST",
        "/v1/tasks/runs",
        headers=creds,
        json={"task_input": {"message": f"go #scenario={scenario}"}},
    )
    assert response.status_code == 201, response.text
    run_id = uuid.UUID(response.json()["run_id"])
    await drain(runtime)
    return run_id


def approval_id(seeded: Any, run_id: uuid.UUID) -> uuid.UUID:
    [(found,)] = rows(seeded, "SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
    assert isinstance(found, uuid.UUID)
    return found


def bob(**claims: Any) -> dict[str, str]:
    return headers(token("bob", ["approver", "oncall"], APPROVALS, **claims))


def counts(seeded: Any) -> tuple[Any, ...]:
    return rows(
        seeded,
        "SELECT (SELECT count(*) FROM tether.runs), (SELECT count(*) FROM tether.approvals),"
        " (SELECT count(*) FROM tether.audit_events), (SELECT count(*) FROM tether.jobs),"
        " (SELECT count(*) FROM tether.ledger_entries)",
    )[0]


LIST = "/v1/approvals/approvals"


async def listed(seeded: Any, tmp_path: Path, caller: dict[str, str]) -> list[str]:
    response = await call(seeded, tmp_path, "GET", LIST, headers=caller)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


# --- list -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t28_an_eligible_approver_lists_a_pending_approval_with_masked_arguments(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path, "rotate")
    # Eligible: the ``vault`` role the rule names.
    vault = headers(token("vera", ["approver", "vault"], APPROVALS))
    response = await call(seeded, tmp_path, "GET", LIST, headers=vault)
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["id"] == str(approval_id(seeded, run_id))
    assert item["tool"] == "rotate_credentials"
    assert item["status"] == "pending"
    assert item["requester"] == "alice"
    assert item["run_id"] == str(run_id)
    assert RAW_VALUE not in response.text  # criterion 6: masked in the list
    assert item["arguments_masked"]["service"] == "checkout"


@pytest.mark.asyncio
async def test_t28_the_list_omits_what_the_caller_may_not_decide(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    own = await submit(runtime, seeded, tmp_path, "restart", requester="bob")  # bob's own
    other = await submit(runtime, seeded, tmp_path, "restart")  # alice's
    await submit(runtime, seeded, tmp_path, "rotate")  # rule names ``vault``: bob lacks it
    foreign = await submit(runtime, seeded, tmp_path, "restart", requester="mallory", ws=OTHER_WS)
    expired = await submit(runtime, seeded, tmp_path, "restart", requester="dan")
    closed = await submit(runtime, seeded, tmp_path, "restart", requester="erin")
    stopped = await submit(runtime, seeded, tmp_path, "restart", requester="fay")
    terminal = await submit(runtime, seeded, tmp_path, "restart", requester="gus")
    rows(
        seeded,
        "UPDATE tether.approvals SET expires_at = now() - interval '1 minute' WHERE run_id = %s"
        " RETURNING 1",
        expired,
    )
    rows(
        seeded,
        "UPDATE tether.approvals SET status = 'closed' WHERE run_id = %s RETURNING 1",
        closed,
    )
    rows(
        seeded,
        "UPDATE tether.runs SET cancel_requested_at = now(), cancel_requested_by = 'fay'"
        " WHERE id = %s RETURNING 1",
        stopped,
    )
    rows(seeded, "UPDATE tether.runs SET status = 'failed' WHERE id = %s RETURNING 1", terminal)
    ids = await listed(seeded, tmp_path, bob())
    assert ids == [str(approval_id(seeded, other))]
    assert str(approval_id(seeded, own)) not in ids
    assert str(approval_id(seeded, foreign)) not in ids
    # Another workspace's approver sees only that workspace's.
    zed = headers(token("zed", ["approver", "oncall"], APPROVALS, ws=OTHER_WS))
    assert await listed(seeded, tmp_path, zed) == [str(approval_id(seeded, foreign))]


@pytest.mark.asyncio
async def test_t28_the_list_is_oldest_first_and_paginated(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    runs = [await submit(runtime, seeded, tmp_path, "restart") for _ in range(3)]
    expected = [str(approval_id(seeded, run)) for run in runs]
    assert await listed(seeded, tmp_path, bob()) == expected
    first = await call(seeded, tmp_path, "GET", LIST, headers=bob(), params={"limit": 2})
    assert [item["id"] for item in first.json()["items"]] == expected[:2]
    cursor = first.json()["next_after"]
    assert cursor == expected[1]
    rest = await call(
        seeded, tmp_path, "GET", LIST, headers=bob(), params={"limit": 2, "after": cursor}
    )
    assert [item["id"] for item in rest.json()["items"]] == expected[2:]
    assert rest.json()["next_after"] is None
    for bad in (0, 201):
        refused = await call(seeded, tmp_path, "GET", LIST, headers=bob(), params={"limit": bad})
        assert refused.status_code == 422


@pytest.mark.asyncio
async def test_t28_a_caller_without_the_approver_role_gets_an_empty_list_or_404(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path)
    plain = headers(token("carol", ["oncall"], APPROVALS))
    response = await call(seeded, tmp_path, "GET", LIST, headers=plain)
    assert response.status_code == 404, response.text
    for url in (
        f"/v1/approvals/approvals/{approval_id(seeded, run_id)}",
        f"/v1/approvals/runs/{run_id}",
    ):
        assert (await call(seeded, tmp_path, "GET", url, headers=plain)).status_code == 404


# --- detail and run evidence ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_t28_detail_shows_the_masked_exact_action_and_run_evidence_follows(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path, "rotate")
    found = approval_id(seeded, run_id)
    vault = headers(token("vera", ["approver", "vault"], APPROVALS))
    detail = await call(seeded, tmp_path, "GET", f"/v1/approvals/approvals/{found}", headers=vault)
    assert detail.status_code == 200, detail.text
    view = detail.json()
    assert view["id"] == str(found)
    assert view["status"] == "pending"
    assert (view["tool"], view["tool_version"]) == ("rotate_credentials", "1.0.0")
    assert (view["requester"], view["agent_id"]) == ("alice", "agent-1")
    assert all(view[name] for name in ("call_hash", "expires_at", "created_at"))
    assert view["preconditions_masked"] == {}
    assert (view["decided_by"], view["decided_at"]) == (None, None)
    assert view["run_id"] == str(run_id)
    assert RAW_VALUE not in detail.text
    evidence = await call(seeded, tmp_path, "GET", f"/v1/approvals/runs/{run_id}", headers=vault)
    assert evidence.status_code == 200, evidence.text
    run = evidence.json()
    assert run["status"] == "awaiting_approval"
    assert (run["requester"], run["agent_id"]) == ("alice", "agent-1")
    assert [a["id"] for a in run["approvals"]] == [str(found)]
    assert run["audit_chain_valid"] is True
    assert run["audit"][0]["event_type"] == "RUN_SUBMITTED"
    assert all(
        set(event) == {"seq", "event_type", "actor", "tool", "policy_version"}
        for event in run["audit"]
    )
    assert {"task_input", "requester_snapshot"}.isdisjoint(run)
    assert RAW_VALUE not in evidence.text
    for text in (detail.text, evidence.text):
        assert "go #scenario" not in text  # the raw task input never leaves


@pytest.mark.asyncio
async def test_t28_a_decided_approval_stays_visible_to_an_eligible_approver_with_its_decision(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path)
    found = approval_id(seeded, run_id)
    done = await call(
        seeded,
        tmp_path,
        "POST",
        f"/v1/approvals/{found}/reject",
        headers=bob(),
        json={"reason": "no"},
    )
    assert done.status_code == 200, done.text
    assert await listed(seeded, tmp_path, bob()) == []
    detail = await call(seeded, tmp_path, "GET", f"/v1/approvals/approvals/{found}", headers=bob())
    assert detail.status_code == 200, detail.text
    view = detail.json()
    assert view["status"] == "rejected"
    assert (view["decided_by"], view["decision_reason"]) == ("bob", "no")
    assert view["decided_at"]
    assert view["policy_version"]


@pytest.mark.asyncio
async def test_t28_detail_and_evidence_are_404_for_everyone_not_eligible(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path)
    found = approval_id(seeded, run_id)
    refused = {
        "requester": headers(token("alice", ["approver", "oncall"], APPROVALS)),
        "no approver role": headers(token("carol", ["oncall"], APPROVALS)),
        "approver without the rule's role": headers(token("dan", ["approver"], APPROVALS)),
        "other workspace": headers(token("zed", ["approver", "oncall"], APPROVALS, ws=OTHER_WS)),
    }
    urls = (
        f"/v1/approvals/approvals/{found}",
        f"/v1/approvals/approvals/{uuid.uuid4()}",
        f"/v1/approvals/runs/{run_id}",
        f"/v1/approvals/runs/{uuid.uuid4()}",
    )
    for name, caller in refused.items():
        for url in urls:
            response = await call(seeded, tmp_path, "GET", url, headers=caller)
            assert response.status_code == 404, (name, url, response.text)
    # A run without any approval is not evidence for an approver.
    plain = await submit(runtime, seeded, tmp_path, "read")
    assert (
        await call(seeded, tmp_path, "GET", f"/v1/approvals/runs/{plain}", headers=bob())
    ).status_code == 404


@pytest.mark.asyncio
async def test_t28_missing_or_invalid_credentials_are_401(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path)
    found = approval_id(seeded, run_id)
    bad = {
        "no token": headers(),
        "bad signature": headers(token("bob", ["approver", "oncall"], APPROVALS, key=STRANGER)),
        "tasks audience": headers(token("bob", ["approver", "oncall"], TASKS)),
        "ops audience": headers(token("bob", ["approver", "oncall"], OPS)),
        "other workspace's token": headers(
            token("bob", ["approver", "oncall"], APPROVALS, ws=OTHER_WS, key=STRANGER)
        ),
    }
    for name, caller in bad.items():
        for url in (LIST, f"{LIST}/{found}", f"/v1/approvals/runs/{run_id}"):
            response = await call(seeded, tmp_path, "GET", url, headers=caller)
            assert response.status_code == 401, (name, url, response.text)
    # The dev shortcut is not extended to the new routes.
    for url in (LIST, f"{LIST}/{found}", f"/v1/approvals/runs/{run_id}"):
        response = await call(seeded, tmp_path, "GET", url, params={"dev_workspace_id": WS})
        assert response.status_code == 401, (url, response.text)


# --- stale policy, no writes, end to end ----------------------------------------------------


@pytest.mark.asyncio
async def test_t28_a_stale_policy_version_answers_503_and_no_data(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path)
    found = approval_id(seeded, run_id)
    rows(
        seeded,
        "UPDATE tether.policy_versions SET active = false WHERE workspace_id = %s RETURNING 1",
        WS,
    )
    for url in (LIST, f"{LIST}/{found}", f"/v1/approvals/runs/{run_id}"):
        response = await call(seeded, tmp_path, "GET", url, headers=bob())
        assert response.status_code == 503, (url, response.text)
        assert "items" not in response.json()
        assert "tool" not in response.text


@pytest.mark.asyncio
async def test_t28_the_read_routes_write_nothing(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path, "rotate")
    found = approval_id(seeded, run_id)
    vault = headers(token("vera", ["approver", "vault"], APPROVALS))
    before = counts(seeded)
    for caller in (vault, bob(), headers()):
        for url in (
            LIST,
            f"{LIST}/{found}",
            f"/v1/approvals/runs/{run_id}",
            f"{LIST}/{uuid.uuid4()}",
        ):
            await call(seeded, tmp_path, "GET", url, headers=caller)
    assert counts(seeded) == before


@pytest.mark.asyncio
async def test_t28_an_approver_goes_from_list_to_detail_to_decision_and_the_run_proceeds(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path)
    [found] = await listed(seeded, tmp_path, bob())
    detail = await call(seeded, tmp_path, "GET", f"{LIST}/{found}", headers=bob())
    assert detail.status_code == 200
    assert detail.json()["tool"] == "restart_service"
    evidence = await call(seeded, tmp_path, "GET", f"/v1/approvals/runs/{run_id}", headers=bob())
    assert evidence.status_code == 200
    approved = await call(
        seeded,
        tmp_path,
        "POST",
        f"/v1/approvals/{found}/approve",
        headers=bob(),
        json={"reason": "looks right"},
    )
    assert approved.status_code == 200, approved.text
    await drain(runtime)
    [(status,)] = rows(seeded, "SELECT status FROM tether.runs WHERE id = %s", run_id)
    assert status == "succeeded"
    assert await listed(seeded, tmp_path, bob()) == []
    assert json.loads(detail.text)["id"] == found
    assert PERMISSIONS  # fixtures share the test_authentication permission set
