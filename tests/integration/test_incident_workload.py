"""W9.3: the incident-remediation workload governed end to end on its real tools.

T-01 runs the shipped reference workload (loaded through its entry point) under the shipped
reference policy (``config/bootstrap/dev.yaml``: its agent, rules and permission maps), the
shipped scenario and the real demo target, with real JWT and API-key credentials and no dev
identity shortcut: submit, policy, approval by a second identity, execution, result and a valid
audit chain. The same world covers the tool-level tests that need the real target's behaviour:
T-09 (output that breaks the schema), T-20 (an unknown outcome of the real idempotent restart),
T-21 (a stale ``expected_config_version``) and T-23 (an adversarial log never changes policy).

Only the issuer keys and the API key are generated here; the policy is the shipped one.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
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
from pydantic import SecretStr
from services.demo_target_service.app import ADVERSARIAL_LOG_LINE, Faults
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.identity import api_keys
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.queue import backoff
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

REPO = Path(__file__).resolve().parents[2]
SHIPPED_SCENARIOS = REPO / "workloads" / "incident_remediation" / "scenarios"
WS, AGENT = "ops-demo", "incident-remediator"
TASKS, APPROVALS, OPS = "tether-tasks", "tether-approvals", "tether-ops"
APP_KEY = api_keys.generate("demo-app")


def _public_pem(key: ed25519.Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


SIGNERS = {aud: ed25519.Ed25519PrivateKey.generate() for aud in (TASKS, APPROVALS, OPS)}


def _iss(audience: str) -> str:
    return f"https://issuer.test/{WS}/{audience}"


def reference_bootstrap() -> BootstrapFile:
    """The shipped dev bootstrap with its identity section replaced by generated test keys.

    Workspace, agent, and the whole policy section (rules, approver and reconciler roles, role
    and agent permissions) are the shipped reference policy, unchanged.
    """
    raw = yaml.safe_load((REPO / "config" / "bootstrap" / "dev.yaml").read_text(encoding="utf-8"))
    [workspace] = raw["workspaces"]
    assert (workspace["id"], workspace["agents"][0]["id"]) == (WS, AGENT)
    workspace["issuers"] = [
        {
            "id": audience,
            "iss": _iss(audience),
            "audiences": [audience],
            "algorithm": "EdDSA",
            "public_keys": {f"{audience}-1": _public_pem(SIGNERS[audience])},
        }
        for audience in SIGNERS
    ]
    workspace["api_keys"] = [
        {
            "key_id": APP_KEY.key_id,
            "salt": APP_KEY.salt.hex(),
            "secret_hash": APP_KEY.secret_hash.hex(),
            "agents": [AGENT],
        }
    ]
    return BootstrapFile.model_validate(raw)


BOOT = reference_bootstrap()


def token(sub: str, roles: Sequence[str], audience: str) -> str:
    now = int(time.time())
    claims = {
        "iss": _iss(audience),
        "aud": audience,
        "sub": sub,
        "workspace": WS,
        "roles": list(roles),
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(
        claims, SIGNERS[audience], algorithm="EdDSA", headers={"kid": f"{audience}-1"}
    )


def task_headers(sub: str = "alice", roles: Sequence[str] = ("operator",)) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(sub, roles, TASKS)}", "X-API-Key": APP_KEY.raw}


def approver_headers(sub: str = "bob", roles: Sequence[str] = ("approver",)) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(sub, roles, APPROVALS)}"}


def reconciler_headers(sub: str = "dave") -> dict[str, str]:
    return {"Authorization": f"Bearer {token(sub, ['reconciler'], OPS)}"}


def scenario(name: str, *turns: str, final: str = "done") -> str:
    lines = [f"name: {name}", "turns:"]
    lines += [f"  - tool_calls: [{turn}]" for turn in turns]
    lines.append(f"  - final: {final}")
    return "\n".join(lines) + "\n"


HEALTH = "{tool: get_service_health, arguments: {service: checkout}}"
LOGS = "{tool: get_service_logs, arguments: {service: checkout}}"
RESTART = "{tool: restart_service, arguments: {service: checkout}}"


def change(version: int, key: str = "pool.size", value: str = "20") -> str:
    return (
        "{tool: update_service_config, arguments: "
        f"{{service: checkout, key: {key}, value: '{value}', expected_config_version: {version}}}}}"
    )


SCENARIOS = {
    "health_only": scenario("health_only", HEALTH, final="checked"),
    "restart": scenario("restart", RESTART, final="restarted"),
    "config_stale": scenario("config_stale", change(0), final="stale"),
    "config_current": scenario("config_current", change(1), final="changed"),
    # A hostile log line, then the model "obeys" it and proposes disabling authentication.
    "adversarial": scenario(
        "adversarial", LOGS, change(1, key="auth.enabled", value="false"), final="proposed"
    ),
}


class RecordingProvider:
    """Wraps the scripted provider and keeps every history it was shown."""

    def __init__(self, inner: ScriptedProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.calls: list[list[NeutralMessage]] = []

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        self.calls.append(list(history))
        return await self.inner.complete(history, manifest)

    def results(self, turn: int) -> list[tuple[str, str]]:
        """(tool-call id, neutral outcome) of the tool results shown at ``turn``."""
        return [(m.call_id or "", str(m.outcome)) for m in self.calls[turn] if m.role == "tool"]


@dataclass
class World:
    db: Any
    tmp_path: Path
    runtime: WorkerRuntime
    provider: RecordingProvider
    target: Any

    @property
    def faults(self) -> Faults:
        return self.target.state.faults  # type: ignore[no-any-return]

    def settings(self) -> Settings:
        return _settings(self.db, self.tmp_path)

    async def call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        app = create_app(self.settings())
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
        ):
            return await http.request(method, url, **kwargs)

    async def submit(self, scenario_name: str, **headers: Any) -> uuid.UUID:
        response = await self.call(
            "POST",
            "/v1/tasks/runs",
            headers=headers or task_headers(),
            json={"task_input": {"message": f"incident #scenario={scenario_name}"}},
        )
        assert response.status_code == 201, response.text
        return uuid.UUID(response.json()["run_id"])

    async def drain(self) -> None:
        for _ in range(60):
            if not await process_next_job(self.runtime):
                return
        raise AssertionError("the queue did not drain")

    async def view(self, run_id: uuid.UUID) -> dict[str, Any]:
        response = await self.call("GET", f"/v1/tasks/runs/{run_id}", headers=task_headers())
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def approve(self, approval_id: Any, **headers: Any) -> httpx.Response:
        return await self.call(
            "POST",
            f"/v1/approvals/{approval_id}/approve",
            headers=headers or approver_headers(),
            json={"reason": "reviewed"},
        )

    async def reject(self, approval_id: Any) -> httpx.Response:
        return await self.call(
            "POST",
            f"/v1/approvals/{approval_id}/reject",
            headers=approver_headers(),
            json={"reason": "not now"},
        )

    def sql(self, statement: str, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.migrator_dsn) as conn:
            return conn.execute(statement, params).fetchall()

    def status(self, run_id: uuid.UUID) -> str:
        return str(self.sql("SELECT status FROM tether.runs WHERE id = %s", run_id)[0][0])

    def approval(self, run_id: uuid.UUID) -> tuple[Any, ...]:
        [row] = self.sql(
            "SELECT id, status, tool, arguments_masked, decided_by FROM tether.approvals"
            " WHERE run_id = %s",
            run_id,
        )
        return row

    def ledger(self, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
        """(ordinal, tool, state, outcome_class, attempts) per entry."""
        return self.sql(
            "SELECT step_ordinal, tool, state, outcome_class, attempts FROM tether.ledger_entries"
            " WHERE run_id = %s ORDER BY step_ordinal",
            run_id,
        )

    def events(self, run_id: uuid.UUID) -> list[str]:
        rows = self.sql(
            "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
        )
        return [event for (event,) in rows]

    async def target_get(self, path: str) -> dict[str, Any]:
        async with demo_client.http_client() as http:
            return dict((await http.get(path)).json())


def _settings(db: Any, tmp_path: Path) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    if not bootstrap_file.exists():
        bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def target(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    app = create_demo_target()
    demo_client.set_transport(httpx.ASGITransport(app=app))
    # Retries are due at once; the PD-13 backoff delays are covered by the retry tests.
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
    yield app
    demo_client.set_transport(None)


@pytest_asyncio.fixture
async def world(seeded: Any, tmp_path: Path, target: Any) -> AsyncIterator[World]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    for shipped in SHIPPED_SCENARIOS.glob("*.yaml"):
        (tmp_path / shipped.name).write_text(shipped.read_text(encoding="utf-8"), encoding="utf-8")
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    provider = RecordingProvider(ScriptedProvider(tmp_path, env="test"))
    # The workload comes from its entry point, as in a deployment: no substituted registry.
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path), bootstrap=BOOT, provider=provider
    )
    registry = runtime.registries[WORKLOAD.name]
    for tool in WORKLOAD.agent.tools:
        assert registry.offered(tool) is not None, tool
    try:
        yield World(seeded.db, tmp_path, runtime, provider, target)
    finally:
        await runtime.close()


async def to_approval(world: World, name: str) -> tuple[uuid.UUID, Any]:
    run_id = await world.submit(name)
    await world.drain()
    assert world.status(run_id) == "awaiting_approval"
    return run_id, world.approval(run_id)[0]


# --- T-01 ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t01_the_reference_workload_runs_end_to_end_with_real_credentials(
    world: World,
) -> None:
    assert (await world.target_get("/services/checkout"))["healthy"] is False
    # 1-2. alice submits with her task token and the workspace API key; no dev identity anywhere.
    submitted = await world.call(
        "POST",
        "/v1/tasks/runs",
        headers=task_headers(),
        json={"task_input": {"message": "checkout is down #scenario=restart_checkout"}},
    )
    assert submitted.status_code == 201, submitted.text
    run_id = uuid.UUID(submitted.json()["run_id"])
    assert "dev_identity" not in submitted.request.content.decode()
    # 3-5. the read is allowed and runs; the restart needs approval, so the run pauses.
    await world.drain()
    assert world.status(run_id) == "awaiting_approval"
    assert world.ledger(run_id) == [(0, "get_service_health", "completed", "applied", 1)]
    approval_id, status, tool, arguments, _ = world.approval(run_id)
    assert (status, tool, arguments) == ("pending", "restart_service", {"service": "checkout"})
    assert (await world.target_get("/services/checkout"))["restarts"] == 0
    # 6. separation of duties: the requester cannot approve her own restart.
    own = await world.approve(approval_id, **approver_headers("alice", ["approver", "operator"]))
    assert own.status_code == 404
    assert world.status(run_id) == "awaiting_approval"
    # 7. a second identity with the approver role approves it.
    approved = await world.approve(approval_id)
    assert approved.status_code == 200, approved.text
    # 8-9. the worker executes the exact approved call against the target; the model reports.
    await world.drain()
    assert world.status(run_id) == "succeeded"
    assert await world.target_get("/services/checkout") == {
        "service": "checkout",
        "healthy": True,
        "restarts": 1,
    }
    # 10-11. the requester reads the result; the audit chain verifies.
    view = await world.view(run_id)
    assert view["status"] == "succeeded"
    assert view["outcome_summary"] == "checkout was unhealthy; restarted it after approval."
    assert [(e["tool"], e["state"]) for e in view["ledger"]] == [
        ("get_service_health", "completed"),
        ("restart_service", "completed"),
    ]
    assert view["ledger"][1]["outcome"] == {"service": "checkout", "healthy": True, "restarts": 1}
    assert [(a["status"], a["decided_by"]) for a in view["approvals"]] == [("approved", "bob")]
    assert view["audit_chain_valid"] is True
    timeline = [event["event_type"] for event in view["audit"]]
    expected = iter(
        [
            "RUN_SUBMITTED",
            "POLICY_DECISION",
            "EXECUTION_AUTHORIZED",
            "DISPATCHED",
            "TOOL_OUTCOME",
            "POLICY_DECISION",
            "APPROVAL_REQUESTED",
            "APPROVAL_DECIDED",
            "EXECUTION_AUTHORIZED",
            "DISPATCHED",
            "TOOL_OUTCOME",
            "RUN_FINISHED",
        ]
    )
    assert all(any(seen == want for seen in timeline) for want in expected)
    assert timeline[0] == "RUN_SUBMITTED"
    assert timeline[-1] == "RUN_FINISHED"
    # The model saw both results in order (read, then the approved write).
    assert world.provider.results(1) == [("call_0_0", "applied")]
    assert world.provider.results(2)[-1] == ("call_1_0", "applied")


@pytest.mark.asyncio
async def test_t01_the_reference_policy_is_what_governs_the_run(world: World) -> None:
    run_id, _ = await to_approval(world, "restart_checkout")
    decisions = dict(
        world.sql(
            "SELECT tool, payload_masked->>'decision' FROM tether.audit_events"
            " WHERE run_id = %s AND event_type = 'POLICY_DECISION'",
            run_id,
        )
    )
    assert decisions == {
        "get_service_health": "allow",
        "restart_service": "require_approval",
    }
    versions = {
        v
        for (v,) in world.sql(
            "SELECT policy_version FROM tether.audit_events WHERE run_id = %s", run_id
        )
        if v
    }
    assert versions == {BOOT.workspaces[0].policy_version}


# --- T-09 ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t09_a_read_that_breaks_its_output_schema_is_rejected_before_state(
    world: World,
) -> None:
    world.faults.malformed_health = True
    run_id = await world.submit("health_only")
    await world.drain()
    # A definitive failure, never retried, with nothing from the bad answer recorded.
    [(ordinal, tool, state, outcome_class, attempts)] = world.ledger(run_id)
    assert (ordinal, tool, state, outcome_class, attempts) == (
        0,
        "get_service_health",
        "failed",
        "failed",
        1,
    )
    [(payload, reason)] = world.sql(
        "SELECT outcome_payload_masked, outcome_reason FROM tether.ledger_entries"
        " WHERE run_id = %s",
        run_id,
    )
    assert payload is None
    assert reason == "failed: output invalid"
    assert world.provider.results(1) == [("call_0_0", "failed")]
    assert world.status(run_id) == "succeeded"
    assert (await world.view(run_id))["audit_chain_valid"] is True


# --- T-20 ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t20_the_real_restart_exhausting_its_retries_is_indeterminate_not_failed(
    world: World,
) -> None:
    run_id, approval_id = await to_approval(world, "restart")
    # Every send is applied-then-lost. Were a send to carry a new key, the target would restart
    # again; one restart across three sends proves the same key was reused.
    world.faults.restart[:] = ["applied_then_unavailable"] * 3
    assert (await world.approve(approval_id)).status_code == 200
    await world.drain()
    [(_, _, state, outcome_class, attempts)] = world.ledger(run_id)
    assert (state, outcome_class, attempts) == ("indeterminate", "unknown", 3)
    assert world.status(run_id) == "awaiting_reconciliation"
    assert world.events(run_id).count("DISPATCHED") == 1  # the first send
    assert world.events(run_id).count("REDISPATCHED") == 2
    assert world.faults.restart == []  # all three sends reached the target
    assert (await world.target_get("/services/checkout"))["restarts"] == 1
    # The model was never told "failed": it has not been called again, and nothing was sent back.
    assert len(world.provider.calls) == 1
    assert world.sql("SELECT count(*) FROM tether.model_calls WHERE run_id = %s", run_id) == [(1,)]
    await world.drain()
    assert (await world.target_get("/services/checkout"))["restarts"] == 1
    # Only a human resolves it; "applied" lets the run continue with an applied result.
    [(entry_id,)] = world.sql("SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id)
    reconciled = await world.call(
        "POST",
        f"/v1/ops/ledger/{entry_id}/reconcile",
        headers=reconciler_headers(),
        json={"outcome": "applied", "justification": "the target shows one restart"},
    )
    assert reconciled.status_code == 200, reconciled.text
    await world.drain()
    assert world.status(run_id) == "succeeded"
    assert world.provider.results(1) == [("call_0_0", "applied")]
    assert (await world.view(run_id))["audit_chain_valid"] is True


# --- T-21 ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t21_a_stale_config_version_is_not_applied_and_the_target_is_unchanged(
    world: World,
) -> None:
    before = await world.target_get("/services/checkout/config")
    run_id, approval_id = await to_approval(world, "config_stale")
    # The approval binds the exact call, expected version included.
    [(arguments,)] = world.sql(
        "SELECT arguments_masked FROM tether.approvals WHERE run_id = %s", run_id
    )
    assert arguments["expected_config_version"] == 0
    assert (await world.approve(approval_id)).status_code == 200
    await world.drain()
    assert world.ledger(run_id) == [(0, "update_service_config", "failed", "not_applied", 1)]
    [(reason,)] = world.sql(
        "SELECT outcome_reason FROM tether.ledger_entries WHERE run_id = %s", run_id
    )
    assert reason.startswith("not applied: precondition_failed")
    assert await world.target_get("/services/checkout/config") == before
    assert world.provider.results(1) == [("call_0_0", "not_applied")]
    assert world.status(run_id) == "succeeded"
    assert (await world.view(run_id))["audit_chain_valid"] is True


@pytest.mark.asyncio
async def test_t21_the_current_config_version_applies_the_approved_change(world: World) -> None:
    run_id, approval_id = await to_approval(world, "config_current")
    assert (await world.approve(approval_id)).status_code == 200
    await world.drain()
    assert world.ledger(run_id) == [(0, "update_service_config", "completed", "applied", 1)]
    config = await world.target_get("/services/checkout/config")
    assert (config["config_version"], config["values"]["pool.size"]) == (2, "20")
    assert world.provider.results(1) == [("call_0_0", "applied")]
    assert world.status(run_id) == "succeeded"


# --- T-23 ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t23_an_adversarial_log_cannot_loosen_policy_and_nothing_applies_unapproved(
    world: World,
) -> None:
    world.faults.adversarial_logs = True
    run_id = await world.submit("adversarial")
    await world.drain()
    # The hostile line reached the read result (it is data), and the model proposed the change.
    [(logs,)] = world.sql(
        "SELECT outcome_payload_masked FROM tether.ledger_entries"
        " WHERE run_id = %s AND tool = 'get_service_logs'",
        run_id,
    )
    assert ADVERSARIAL_LOG_LINE in logs["lines"]
    # Policy, not the model, is the boundary: the change still needs an approval, and no write ran.
    decisions = world.sql(
        "SELECT tool, payload_masked->>'decision' FROM tether.audit_events"
        " WHERE run_id = %s AND event_type = 'POLICY_DECISION' ORDER BY seq",
        run_id,
    )
    assert decisions == [
        ("get_service_logs", "allow"),
        ("update_service_config", "require_approval"),
    ]
    assert world.status(run_id) == "awaiting_approval"
    assert [tool for _, tool, *_ in world.ledger(run_id)] == ["get_service_logs"]
    config = await world.target_get("/services/checkout/config")
    assert (config["config_version"], config["values"]["auth.enabled"]) == (1, "true")
    # The approver sees the canonical, masked arguments the approval binds, and no log text or
    # model rationale (OD-7: model rationale is not exposed to the approver).
    approval_id = world.approval(run_id)[0]
    detail = await world.call(
        "GET", f"/v1/approvals/approvals/{approval_id}", headers=approver_headers()
    )
    assert detail.status_code == 200, detail.text
    shown = detail.json()
    assert shown["tool"] == "update_service_config"
    assert shown["arguments_masked"] == {
        "service": "checkout",
        "key": "auth.enabled",
        "value": "false",
        "expected_config_version": 1,
    }
    assert ADVERSARIAL_LOG_LINE not in detail.text
    # Rejecting it leaves the target untouched and the run ends cleanly.
    assert (await world.reject(approval_id)).status_code == 200
    await world.drain()
    assert world.status(run_id) == "succeeded"
    assert world.ledger(run_id) == [(0, "get_service_logs", "completed", "applied", 1)]
    assert await world.target_get("/services/checkout/config") == config
    assert (await world.view(run_id))["audit_chain_valid"] is True


@pytest.mark.asyncio
async def test_t23_approving_the_adversarial_change_is_the_only_way_it_applies(
    world: World,
) -> None:
    world.faults.adversarial_logs = True
    run_id = await world.submit("adversarial")
    await world.drain()
    approval_id = world.approval(run_id)[0]
    assert (await world.approve(approval_id)).status_code == 200
    await world.drain()
    config = await world.target_get("/services/checkout/config")
    assert (config["config_version"], config["values"]["auth.enabled"]) == (2, "false")
    assert world.approval(run_id)[4] == "bob"
