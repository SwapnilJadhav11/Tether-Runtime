"""T-18 / T-19 / T-24 (core cases) / T-26: real authentication and route authorization.

ADR-0013: the expected audience comes from the route; JWTs are validated against the workspace's
platform-controlled issuers before anything else (``401``); tasks routes also need the workspace
API key, which resolves the workspace and the allowed agents server-side; roles come only from the
validated token; anything out of scope or not owned is ``404``. The dev/test identity shortcut is
accepted only when ``TETHER_ENV`` is dev or test, and never instead of real credentials.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
import pytest
import pytest_asyncio
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import BaseModel, SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.identity import api_keys
from tether.policy.config import BootstrapFile
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS, OTHER_WS = "ws-a", "ws-b"
TASKS, APPROVALS, OPS = "tether-tasks", "tether-approvals", "tether-ops"


def _public_pem(key: ed25519.Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


# One signing key per (workspace, audience); the private halves live only in this test.
SIGNERS = {
    (ws, aud): ed25519.Ed25519PrivateKey.generate()
    for ws in (WS, OTHER_WS)
    for aud in (TASKS, APPROVALS, OPS)
}
STRANGER = ed25519.Ed25519PrivateKey.generate()
APP_KEY = api_keys.generate("app-a")
OTHER_APP_KEY = api_keys.generate("app-b")


def _iss(ws: str, aud: str) -> str:
    return f"https://issuer.test/{ws}/{aud}"


def _workspace(
    ws: str, agents: Sequence[str], key: api_keys.IssuedKey, bound: Sequence[str]
) -> dict[str, Any]:
    return {
        "id": ws,
        "agents": [{"id": agent, "workload": WORKLOAD.name} for agent in agents],
        "issuers": [
            {
                "id": aud,
                "iss": _iss(ws, aud),
                "audiences": [aud],
                "algorithm": "EdDSA",
                "public_keys": {f"{aud}-1": _public_pem(SIGNERS[(ws, aud)])},
            }
            for aud in (TASKS, APPROVALS, OPS)
        ],
        "api_keys": [
            {
                "key_id": key.key_id,
                "salt": key.salt.hex(),
                "secret_hash": key.secret_hash.hex(),
                "agents": list(bound),
            }
        ],
        "policy": {
            "rules": [
                {"tool": "get_service_health", "decision": "allow"},
                {
                    "tool": "restart_service",
                    "decision": "require_approval",
                    "approver_roles": ["oncall"],
                },
                {"tool": "probe_nonidem", "decision": "allow"},
            ],
            "reconciliation": {"reconciler_roles": ["sre"]},
        },
    }


BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            _workspace(WS, ["agent-1", "agent-2"], APP_KEY, ["agent-1"]),
            _workspace(OTHER_WS, ["agent-b"], OTHER_APP_KEY, ["agent-b"]),
        ]
    }
)
SCENARIOS = {
    "read": (
        "name: read\nturns:\n"
        "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
        "  - final: read done\n"
    ),
    "restart": (
        "name: restart\nturns:\n"
        "  - tool_calls: [{tool: restart_service, arguments: {service: checkout}}]\n"
        "  - final: restart done\n"
    ),
    "nonidem": (
        "name: nonidem\nturns:\n"
        "  - tool_calls: [{tool: probe_nonidem, arguments: {target: a}}]\n"
        "  - final: probe done\n"
    ),
}


# --- a non-idempotent probe whose connection drops: an indeterminate write -------------------


class ProbeIn(BaseModel):
    target: str


class ProbeOut(BaseModel):
    target: str


async def _drop(args: ProbeIn, ctx: ToolContext) -> ProbeOut:
    raise httpx.RemoteProtocolError("connection dropped after the request was sent")


PROBE = ToolDescriptor(
    name="probe_nonidem",
    version="1.0.0",
    description="Test-only write without idempotency support.",
    input_model=ProbeIn,
    output_model=ProbeOut,
    effect="write",
    idempotency="unsupported",
    handler=_drop,
)
TEST_WORKLOAD = Workload(
    name=WORKLOAD.name,
    tools=(*WORKLOAD.tools, PROBE),
    agent=AgentDefinition(system_prompt="test", tools=(*WORKLOAD.agent.tools, PROBE.name)),
)


# --- credentials ----------------------------------------------------------------------------


def token(
    sub: str,
    roles: Sequence[str] = (),
    audience: str = TASKS,
    *,
    ws: str = WS,
    key: Any = None,
    **claims: Any,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": _iss(ws, audience),
        "aud": audience,
        "sub": sub,
        "workspace": ws,
        "roles": list(roles),
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    signer = key if key is not None else SIGNERS[(ws, audience)]
    return jwt.encode(payload, signer, algorithm="EdDSA", headers={"kid": f"{audience}-1"})


def headers(bearer: str | None = None, api_key: str | None = None) -> dict[str, str]:
    found: dict[str, str] = {}
    if bearer is not None:
        found["Authorization"] = f"Bearer {bearer}"
    if api_key is not None:
        found["X-API-Key"] = api_key
    return found


def alice(api_key: str | None = APP_KEY.raw) -> dict[str, str]:
    return headers(token("alice", ["operator"]), api_key)


# --- fixtures and helpers -------------------------------------------------------------------


def _settings(db: Any, tmp_path: Path, env: str = "test") -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env=env,
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def demo_target() -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    yield
    demo_client.set_transport(None)


@pytest_asyncio.fixture
async def runtime(seeded: Any, tmp_path: Path) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path),
        bootstrap=BOOT,
        provider=ScriptedProvider(tmp_path, env="test"),
    )
    runtime.registries[WORKLOAD.name] = ToolRegistry(TEST_WORKLOAD)
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


def body(scenario: str = "read", **extra: Any) -> dict[str, Any]:
    return {"task_input": {"message": f"go #scenario={scenario}"}, **extra}


async def submit(seeded: Any, tmp_path: Path, scenario: str = "read") -> uuid.UUID:
    response = await call(
        seeded, tmp_path, "POST", "/v1/tasks/runs", headers=alice(), json=body(scenario)
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(60):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def counts(seeded: Any) -> tuple[Any, ...]:
    """(runs, audit events, jobs) across the database: a rejected request writes none."""
    return rows(
        seeded,
        "SELECT (SELECT count(*) FROM tether.runs), (SELECT count(*) FROM tether.audit_events),"
        " (SELECT count(*) FROM tether.jobs)",
    )[0]


def run_row(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return rows(
        seeded,
        "SELECT workspace_id, agent_id, requester_snapshot, status, cancel_requested_by"
        " FROM tether.runs WHERE id = %s",
        run_id,
    )[0]


# --- tasks: token + API key -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_t18_a_valid_task_token_and_api_key_submit_a_run(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path)
    # The workspace comes from the key, the agent from its binding, the requester from the token.
    assert run_row(seeded, run_id)[:4] == (
        WS,
        "agent-1",
        {"sub": "alice", "roles": ["operator"]},
        "queued",
    )
    await drain(runtime)
    assert run_row(seeded, run_id)[3] == "succeeded"


@pytest.mark.asyncio
async def test_t18_caller_supplied_identity_in_the_body_is_ignored(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    forged = {
        "workspace_id": OTHER_WS,
        "agent_id": "agent-b",
        "requester": {"sub": "mallory", "roles": ["approver", "workspace_admin"]},
    }
    response = await call(
        seeded, tmp_path, "POST", "/v1/tasks/runs", headers=alice(), json=body(dev_identity=forged)
    )
    assert response.status_code == 201, response.text
    run_id = uuid.UUID(response.json()["run_id"])
    assert run_row(seeded, run_id)[:3] == (WS, "agent-1", {"sub": "alice", "roles": ["operator"]})


@pytest.mark.parametrize(
    "api_key",
    [
        None,
        "",
        api_keys.generate("app-a").raw,  # right key id, wrong secret
        api_keys.generate("app-unknown").raw,
        "not-an-api-key",
        OTHER_APP_KEY.raw,  # a valid key of another workspace
    ],
)
@pytest.mark.asyncio
async def test_t18_a_missing_wrong_or_foreign_api_key_is_rejected(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, api_key: str | None
) -> None:
    before = counts(seeded)
    response = await call(
        seeded, tmp_path, "POST", "/v1/tasks/runs", headers=alice(api_key), json=body()
    )
    assert response.status_code == 401, response.text
    assert counts(seeded) == before


@pytest.mark.asyncio
async def test_t18_an_agent_outside_the_api_key_binding_is_rejected(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    before = counts(seeded)
    for agent in ("agent-2", "agent-b", "no-such-agent"):
        response = await call(
            seeded, tmp_path, "POST", "/v1/tasks/runs", headers=alice(), json=body(agent_id=agent)
        )
        assert response.status_code == 401, (agent, response.text)
    assert counts(seeded) == before
    named = await call(
        seeded, tmp_path, "POST", "/v1/tasks/runs", headers=alice(), json=body(agent_id="agent-1")
    )
    assert named.status_code == 201, named.text


def _hs256() -> str:
    claims = {
        "iss": _iss(WS, TASKS),
        "aud": TASKS,
        "sub": "alice",
        "workspace": WS,
        "exp": int(time.time()) + 300,
    }
    return jwt.encode(claims, "s" * 32, algorithm="HS256", headers={"kid": f"{TASKS}-1"})


INVALID_TOKENS: dict[str, Any] = {
    "no token": lambda: None,
    "bad signature": lambda: token("alice", ["operator"], key=STRANGER),
    "wrong audience": lambda: token("alice", ["operator"], APPROVALS),
    "wrong issuer": lambda: token("alice", ["operator"], iss="https://issuer.test/elsewhere"),
    "expired": lambda: token("alice", ["operator"], exp=int(time.time()) - 60),
    "unsupported algorithm": _hs256,
    "other workspace's token": lambda: token("mallory", ["operator"], ws=OTHER_WS),
}


@pytest.mark.parametrize("case", INVALID_TOKENS)
@pytest.mark.asyncio
async def test_t18_an_invalid_token_is_rejected_before_anything_is_written(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, case: str
) -> None:
    before = counts(seeded)
    response = await call(
        seeded,
        tmp_path,
        "POST",
        "/v1/tasks/runs",
        headers=headers(INVALID_TOKENS[case](), APP_KEY.raw),
        json=body(),
    )
    assert response.status_code == 401, response.text
    assert response.headers.get("www-authenticate") == "Bearer"
    assert counts(seeded) == before


@pytest.mark.asyncio
async def test_t26_reading_and_cancelling_a_run_is_for_its_requester_only(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "restart")
    url = f"/v1/tasks/runs/{run_id}"
    assert (await call(seeded, tmp_path, "GET", url, headers=alice())).status_code == 200
    carol = headers(token("carol", ["operator"]), APP_KEY.raw)
    mallory = headers(token("mallory", ["operator"], ws=OTHER_WS), OTHER_APP_KEY.raw)
    for caller in (carol, mallory):
        assert (await call(seeded, tmp_path, "GET", url, headers=caller)).status_code == 404
        cancelled = await call(seeded, tmp_path, "POST", f"{url}/cancel", headers=caller)
        assert cancelled.status_code == 404
    assert run_row(seeded, run_id)[4] is None
    cancelled = await call(seeded, tmp_path, "POST", f"{url}/cancel", headers=alice())
    assert cancelled.status_code == 202, cancelled.text
    assert run_row(seeded, run_id)[4] == "alice"


# --- approvals: tether-approvals token, approver role + policy, never the requester ----------


async def _awaiting_approval(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> tuple[uuid.UUID, Any]:
    run_id = await submit(seeded, tmp_path, "restart")
    await drain(runtime)
    [(approval_id,)] = rows(seeded, "SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
    return run_id, approval_id


@pytest.mark.asyncio
async def test_t19_an_approver_needs_the_approvals_audience_the_role_and_not_to_be_the_requester(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, approval_id = await _awaiting_approval(runtime, seeded, tmp_path)
    url = f"/v1/approvals/{approval_id}/approve"
    refused = {
        "requester": (headers(token("alice", ["approver", "oncall"], APPROVALS)), 404),
        "no approver role": (headers(token("bob", ["oncall"], APPROVALS)), 404),
        "not eligible by policy": (headers(token("bob", ["approver"], APPROVALS)), 404),
        "other workspace": (
            headers(token("mallory", ["approver", "oncall"], APPROVALS, ws=OTHER_WS)),
            404,
        ),
        "tasks token": (headers(token("bob", ["approver"])), 401),
        "ops token": (headers(token("bob", ["approver"], OPS)), 401),
        "no token": (headers(), 401),
    }
    for name, (caller, expected) in refused.items():
        response = await call(seeded, tmp_path, "POST", url, headers=caller, json={"reason": "ok"})
        assert response.status_code == expected, (name, response.text)
    # Roles in the body are ignored: bob's token has no approver role.
    forged = {
        "reason": "ok",
        "dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]},
    }
    bob_oncall = headers(token("bob", ["oncall"], APPROVALS))
    response = await call(seeded, tmp_path, "POST", url, headers=bob_oncall, json=forged)
    assert response.status_code == 404, response.text
    bob = headers(token("bob", ["approver", "oncall"], APPROVALS))
    approved = await call(seeded, tmp_path, "POST", url, headers=bob, json={"reason": "ok"})
    assert approved.status_code == 200, approved.text
    [(decided_by,)] = rows(
        seeded, "SELECT decided_by FROM tether.approvals WHERE id = %s", approval_id
    )
    assert decided_by == "bob"
    await drain(runtime)
    assert run_row(seeded, run_id)[3] == "succeeded"


# --- ops: reconcile and admin cancel -------------------------------------------------------


@pytest.mark.asyncio
async def test_t25_a_reconciler_needs_the_ops_audience_the_role_and_not_to_be_the_requester(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "nonidem")
    await drain(runtime)
    [(ledger_id, state)] = rows(
        seeded, "SELECT id, state FROM tether.ledger_entries WHERE run_id = %s", run_id
    )
    assert state == "indeterminate"
    url = f"/v1/ops/ledger/{ledger_id}/reconcile"
    payload = {"outcome": "not_applied", "justification": "checked the target"}
    refused = {
        "requester": (headers(token("alice", ["reconciler", "sre"], OPS)), 404),
        "no reconciler role": (headers(token("erin", ["auditor", "sre"], OPS)), 404),
        "not eligible by policy": (headers(token("dave", ["reconciler"], OPS)), 404),
        "other workspace": (
            headers(token("mallory", ["reconciler", "sre"], OPS, ws=OTHER_WS)),
            404,
        ),
        "approvals token": (headers(token("dave", ["reconciler"], APPROVALS)), 401),
        "no token": (headers(), 401),
    }
    for name, (caller, expected) in refused.items():
        response = await call(seeded, tmp_path, "POST", url, headers=caller, json=payload)
        assert response.status_code == expected, (name, response.text)
    dave = headers(token("dave", ["reconciler", "sre"], OPS))
    done = await call(seeded, tmp_path, "POST", url, headers=dave, json=payload)
    assert done.status_code == 200, done.text
    [(resolved_by,)] = rows(
        seeded, "SELECT resolved_by FROM tether.ledger_entries WHERE id = %s", ledger_id
    )
    assert resolved_by == "dave"


@pytest.mark.asyncio
async def test_t26_a_workspace_admin_cancels_any_run_through_the_ops_route(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, approval_id = await _awaiting_approval(runtime, seeded, tmp_path)
    url = f"/v1/ops/runs/{run_id}/cancel"
    refused = {
        "auditor": (headers(token("erin", ["auditor"], OPS)), 404),
        "approver": (headers(token("bob", ["approver"], OPS)), 404),
        "other workspace": (
            headers(token("mallory", ["workspace_admin"], OPS, ws=OTHER_WS)),
            404,
        ),
        "tasks token": (alice(), 401),
        "approvals token": (headers(token("frank", ["workspace_admin"], APPROVALS)), 401),
    }
    for name, (caller, expected) in refused.items():
        response = await call(seeded, tmp_path, "POST", url, headers=caller)
        assert response.status_code == expected, (name, response.text)
    frank = headers(token("frank", ["workspace_admin"], OPS))
    response = await call(seeded, tmp_path, "POST", url, headers=frank)
    assert response.status_code == 202, response.text
    unknown = await call(
        seeded, tmp_path, "POST", f"/v1/ops/runs/{uuid.uuid4()}/cancel", headers=frank
    )
    assert unknown.status_code == 404
    await drain(runtime)
    assert run_row(seeded, run_id)[3:] == ("cancelled", "frank")
    [(status,)] = rows(seeded, "SELECT status FROM tether.approvals WHERE id = %s", approval_id)
    assert status == "closed"


# --- the dev/test shortcut, and production ------------------------------------------------


@pytest.mark.asyncio
async def test_the_dev_shortcut_still_works_in_test(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    dev = {"workspace_id": WS, "agent_id": "agent-2", "requester": {"sub": "alice", "roles": []}}
    response = await call(seeded, tmp_path, "POST", "/v1/tasks/runs", json=body(dev_identity=dev))
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_t18_production_accepts_real_credentials_and_refuses_the_dev_shortcut(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path)
    dev = {"workspace_id": WS, "sub": "alice", "roles": ["approver", "reconciler"]}
    submitter = {"workspace_id": WS, "agent_id": "agent-1", "requester": {"sub": "alice"}}
    attempts: list[tuple[str, str, dict[str, Any]]] = [
        ("POST", "/v1/tasks/runs", {"json": body(dev_identity=submitter)}),
        ("GET", f"/v1/tasks/runs/{run_id}", {"params": {"dev_workspace_id": WS}}),
        ("POST", f"/v1/tasks/runs/{run_id}/cancel", {"json": {"dev_identity": dev}}),
        ("POST", f"/v1/approvals/{uuid.uuid4()}/approve", {"json": {"dev_identity": dev}}),
        (
            "POST",
            f"/v1/ops/ledger/{uuid.uuid4()}/reconcile",
            {"json": {"dev_identity": dev, "outcome": "applied", "justification": "x"}},
        ),
        ("POST", f"/v1/ops/runs/{run_id}/cancel", {"json": {"dev_identity": dev}}),
    ]
    before = counts(seeded)
    for method, url, kwargs in attempts:
        response = await call(seeded, tmp_path, method, url, env="prod", **kwargs)
        assert response.status_code == 401, (url, response.text)
    assert counts(seeded) == before
    real = await call(
        seeded, tmp_path, "POST", "/v1/tasks/runs", env="prod", headers=alice(), json=body()
    )
    assert real.status_code == 201, real.text
    read = await call(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", env="prod", headers=alice()
    )
    assert read.status_code == 200, read.text


@pytest.mark.asyncio
async def test_tokens_and_api_keys_are_never_logged(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bearer = token("alice", ["operator"])
    bad = token("alice", ["operator"], key=STRANGER)
    for credential in (bearer, bad):
        await call(
            seeded,
            tmp_path,
            "POST",
            "/v1/tasks/runs",
            headers=headers(credential, APP_KEY.raw),
            json=body(),
        )
    secret = APP_KEY.raw.rsplit("_", 1)[1]
    for value in (bearer, bad, APP_KEY.raw, secret):
        assert value not in caplog.text
