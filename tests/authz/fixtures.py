"""T-24 fixtures: two workspaces, each with its own issuers and API key, and a worker to drive runs.

``ops-demo`` is the workspace under test; ``fixture-b`` is the second fixture workspace whose
credentials must never reach ``ops-demo`` data (and the reverse). Every private key is generated
when this module is imported and lives only in the test process; the bootstrap the tests apply
carries public keys and the API-key hash, like a real deployment (ADR-0014).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
import jwt
import psycopg
import pytest_asyncio
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI
from pydantic import BaseModel, SecretStr
from tests.integration.test_authentication import SCENARIOS as BASE_SCENARIOS
from tests.integration.test_authentication import TEST_WORKLOAD
from workloads.incident_remediation import WORKLOAD

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.identity import api_keys
from tether.policy.config import BootstrapFile
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, Sensitive, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

OPS_DEMO, FIXTURE_B = "ops-demo", "fixture-b"
WORKSPACES = (OPS_DEMO, FIXTURE_B)
AGENT = {OPS_DEMO: "agent-1", FIXTURE_B: "agent-b"}
TASKS, APPROVALS, OPS = "tether-tasks", "tether-approvals", "tether-ops"
AUDIENCES = (TASKS, APPROVALS, OPS)
PERMISSIONS = ("service:read", "service:restart", "probe:write")


# A write with a ``Sensitive`` argument: every view must show it masked, never raw (ADR-0017).
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
FIXTURE_WORKLOAD = Workload(
    name=TEST_WORKLOAD.name,
    tools=(*TEST_WORKLOAD.tools, ROTATE),
    agent=AgentDefinition(system_prompt="test", tools=(*TEST_WORKLOAD.agent.tools, ROTATE.name)),
)
SCENARIOS = {
    **BASE_SCENARIOS,
    "rotate": (
        "name: rotate\nturns:\n"
        "  - tool_calls: [{tool: rotate_credentials,"
        f" arguments: {{service: checkout, api_token: {RAW_VALUE}}}}}]\n"
        "  - final: rotated\n"
    ),
}


def _public_pem(key: ed25519.Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


# One signing key per (workspace, audience), plus a key no workspace trusts.
SIGNERS = {
    (ws, aud): ed25519.Ed25519PrivateKey.generate() for ws in WORKSPACES for aud in AUDIENCES
}
STRANGER = ed25519.Ed25519PrivateKey.generate()
APP_KEYS = {
    OPS_DEMO: api_keys.generate("app-demo"),
    FIXTURE_B: api_keys.generate("app-fixture-b"),
}


def _iss(ws: str, audience: str) -> str:
    return f"https://issuer.test/{ws}/{audience}"


def _workspace(ws: str) -> dict[str, Any]:
    key = APP_KEYS[ws]
    return {
        "id": ws,
        "agents": [{"id": AGENT[ws], "workload": WORKLOAD.name}],
        "issuers": [
            {
                "id": aud,
                "iss": _iss(ws, aud),
                "audiences": [aud],
                "algorithm": "EdDSA",
                "public_keys": {f"{aud}-1": _public_pem(SIGNERS[(ws, aud)])},
            }
            for aud in AUDIENCES
        ],
        "api_keys": [
            {
                "key_id": key.key_id,
                "salt": key.salt.hex(),
                "secret_hash": key.secret_hash.hex(),
                "agents": [AGENT[ws]],
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
                {
                    "tool": "rotate_credentials",
                    "decision": "require_approval",
                    "approver_roles": ["oncall"],
                },
            ],
            "reconciliation": {"reconciler_roles": ["sre"]},
            "role_permissions": {"operator": list(PERMISSIONS)},
            "agent_permissions": {AGENT[ws]: list(PERMISSIONS)},
        },
    }


BOOT = BootstrapFile.model_validate({"workspaces": [_workspace(ws) for ws in WORKSPACES]})


def token(
    ws: str,
    audience: str,
    sub: str,
    roles: Sequence[str] = (),
    *,
    key: Any = None,
    **claims: Any,
) -> str:
    """A token for ``ws`` and ``audience``; ``claims`` override or add (``exp``, ``iss`` ...)."""
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


def requester(ws: str = OPS_DEMO, sub: str = "alice") -> dict[str, str]:
    """A run's requester: a ``tether-tasks`` token and the workspace API key."""
    return headers(token(ws, TASKS, sub, ["operator"]), APP_KEYS[ws].raw)


def person(ws: str, audience: str, sub: str, roles: Sequence[str]) -> dict[str, str]:
    return headers(token(ws, audience, sub, roles))


def settings_for(db: Any, tmp_path: Path, env: str = "test") -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env=env,
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


@dataclass
class World:
    """A migrated database, both workspaces applied from bootstrap, and a worker to drive runs."""

    db: Any
    tmp_path: Path
    runtime: WorkerRuntime

    @asynccontextmanager
    async def serving(self, env: str = "test") -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
        """One app (and its lifespan) serving several requests."""
        app = create_app(settings_for(self.db, self.tmp_path, env))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
        ):
            yield app, http

    async def request(
        self, method: str, url: str, *, env: str = "test", **kwargs: Any
    ) -> httpx.Response:
        async with self.serving(env) as (_, http):
            return await http.request(method, url, **kwargs)

    async def drain(self) -> None:
        for _ in range(60):
            if not await process_next_job(self.runtime):
                return
        raise AssertionError("the queue did not drain")

    async def submit(
        self, ws: str = OPS_DEMO, scenario: str = "restart", sub: str = "alice"
    ) -> uuid.UUID:
        """Submit with real credentials and let the worker run it as far as it goes."""
        response = await self.request(
            "POST",
            "/v1/tasks/runs",
            headers=requester(ws, sub),
            json={"task_input": {"message": f"go #scenario={scenario}"}},
        )
        assert response.status_code == 201, response.text
        run_id = uuid.UUID(response.json()["run_id"])
        await self.drain()
        return run_id

    def rows(self, statement: str, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.migrator_dsn) as conn:
            return conn.execute(statement, params).fetchall()

    def approval_id(self, run_id: uuid.UUID) -> uuid.UUID:
        [(found,)] = self.rows("SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
        assert isinstance(found, uuid.UUID)
        return found

    def ledger_id(self, run_id: uuid.UUID) -> uuid.UUID:
        [(found,)] = self.rows("SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id)
        assert isinstance(found, uuid.UUID)
        return found

    def counts(self) -> tuple[Any, ...]:
        """Rows a rejected request must not change."""
        return self.rows(
            "SELECT (SELECT count(*) FROM tether.runs), (SELECT count(*) FROM tether.approvals),"
            " (SELECT count(*) FROM tether.audit_events), (SELECT count(*) FROM tether.jobs),"
            " (SELECT count(*) FROM tether.ledger_entries),"
            " (SELECT count(*) FROM tether.access_events)"
        )[0]


@pytest_asyncio.fixture
async def world(fresh_db: Any, tmp_path: Path) -> AsyncIterator[World]:
    from services.demo_target_service.app import create_app as create_demo_target
    from workloads.incident_remediation import client as demo_client

    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    apply_bootstrap(fresh_db.migrator_dsn, BOOT)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    runtime = await WorkerRuntime.open(
        settings_for(fresh_db, tmp_path),
        bootstrap=BOOT,
        provider=ScriptedProvider(tmp_path, env="test"),
    )
    runtime.registries[WORKLOAD.name] = ToolRegistry(FIXTURE_WORKLOAD)
    try:
        yield World(fresh_db, tmp_path, runtime)
    finally:
        await runtime.close()
        demo_client.set_transport(None)
