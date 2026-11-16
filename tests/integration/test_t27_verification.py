"""W5.1, T-27: bounded automatic verification of unknown outcomes (ADR-0019 rules 7-12, ADR-0022).

The gate matrix, end to end through the worker: a write whose outcome is unknown becomes
``indeterminate`` and, when its pinned version declares ``verify``, a ``verify_outcome`` job probes
the target's read-back. A definitive answer resolves the entry (``verified_*``) and the run goes on
with a typed result carrying the verification marker and masked evidence. Every refusal (no
declaration, policy, a changed or stale policy, the pinned version gone, a stop, exhausted
attempts, other arguments) ends verification with ``VERIFICATION_EXHAUSTED`` and leaves the entry
to a person. A human reconciliation that lands first always wins.

The target is a test-local key-value store. Writes are scripted to apply-then-fail (an unknown
outcome that did apply) or to fail without applying; probes read the store back.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from pydantic import BaseModel, SecretStr
from workloads.incident_remediation import WORKLOAD

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.queue import backoff
from tether.registry.masking import MASK
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import (
    AgentDefinition,
    Sensitive,
    ToolContext,
    ToolDescriptor,
    VerificationResult,
    VerifyContext,
    VerifyDeclaration,
    Workload,
)
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS, AGENT = "ws-ver", "agent-ver"
CANARY = "canary-verification-receipt"
NOT_VERIFY = ["advance_run", "execute_tool", "run_deadline", "expire_approval"]


# --- a test-local key-value target with a read-back ----------------------------------------------


class PutIn(BaseModel):
    key: str
    value: str


class PutOut(BaseModel):
    key: str
    value: str


class Seen(BaseModel):
    """Verification evidence; the receipt is Sensitive and must never appear unmasked."""

    key: str
    present: bool
    receipt: Annotated[str, Sensitive]


@dataclass
class KvTarget:
    """``writes`` scripts each write: ``apply_error`` (applies, then no answer) or ``error`` (no
    answer, nothing applied). ``probes`` scripts each probe: ``read`` (reads the store back),
    ``inconclusive``, ``error``, ``slow`` or ``invalid``. ``mutations`` counts every change to the
    store; probes must never add to it."""

    store: dict[str, str] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)
    sends: int = 0
    mutations: int = 0
    probe_calls: list[tuple[PutIn, VerifyContext]] = field(default_factory=list)
    during_probe: Callable[[], Awaitable[None]] | None = None

    def reset(self) -> None:
        self.store.clear()
        self.writes.clear()
        self.probes.clear()
        self.probe_calls.clear()
        self.sends = self.mutations = 0
        self.during_probe = None

    async def put(self, args: PutIn, ctx: ToolContext) -> PutOut:
        self.sends += 1
        step = self.writes.pop(0) if self.writes else "apply_error"
        if step == "apply_error":
            self.store[args.key] = args.value
            self.mutations += 1
        raise httpx.ReadTimeout("no response")

    async def probe(self, args: PutIn, ctx: VerifyContext) -> VerificationResult:
        self.probe_calls.append((args, ctx))
        if self.during_probe is not None:
            await self.during_probe()
        step = self.probes.pop(0) if self.probes else "read"
        if step == "error":
            raise httpx.ConnectError("target down")
        if step == "slow":
            await asyncio.sleep(30)
        if step == "invalid":
            return VerificationResult("confirmed_applied", PutOut(key="x", value="y"))
        present = self.store.get(args.key) == args.value
        evidence = Seen(key=args.key, present=present, receipt=CANARY)
        if step == "inconclusive":
            return VerificationResult("inconclusive", evidence)
        kind = "confirmed_applied" if present else "confirmed_not_applied"
        return VerificationResult(kind, evidence)


TARGET = KvTarget()


async def _put(args: Any, ctx: ToolContext) -> PutOut:
    return await TARGET.put(args, ctx)


async def _probe(args: Any, ctx: VerifyContext) -> VerificationResult:
    return await TARGET.probe(args, ctx)


VERIFY = VerifyDeclaration(probe=_probe, evidence_model=Seen, required_permissions=("kv:read",))


def kv_tool(name: str, *, version: str = "1.0.0", **extra: Any) -> ToolDescriptor:
    values: dict[str, Any] = {
        "name": name,
        "version": version,
        "description": f"Test-only {name}.",
        "input_model": PutIn,
        "output_model": PutOut,
        "effect": "write",
        "handler": _put,
        "risk": "low",
        "required_permissions": ("kv:write",),
        "idempotency": "unsupported",
        "timeout_seconds": 0.3,
        "verify": VERIFY,
        **extra,
    }
    return ToolDescriptor(**values)


KV_PUT = kv_tool("kv_put")
KV_PUT_GATED = kv_tool("kv_put_gated", risk="high")
KV_PUT_PLAIN = kv_tool("kv_put_plain", verify=None)
KV_PUT_IDEM = kv_tool("kv_put_idem", idempotency="supported")
TOOLS = (KV_PUT, KV_PUT_GATED, KV_PUT_PLAIN, KV_PUT_IDEM)


def workload(*tools: ToolDescriptor) -> Workload:
    return Workload(
        name=WORKLOAD.name,
        tools=(*WORKLOAD.tools, *tools),
        agent=AgentDefinition(
            system_prompt="test", tools=(*WORKLOAD.agent.tools, *(t.name for t in tools))
        ),
    )


TEST_WORKLOAD = workload(*TOOLS)


def policy(**overrides: Any) -> dict[str, Any]:
    return {
        "rules": [
            {"tool": "kv_put", "decision": "allow"},
            {"tool": "kv_put_plain", "decision": "allow"},
            {"tool": "kv_put_idem", "decision": "allow"},
            {
                "tool": "kv_put_gated",
                "decision": "require_approval",
                "approver_roles": ["approver"],
            },
        ],
        "reconciliation": {"reconciler_roles": ["reconciler"]},
        "role_permissions": {"operator": ["kv:read", "kv:write"], "writer": ["kv:write"]},
        "agent_permissions": {AGENT: ["kv:read", "kv:write"]},
        **overrides,
    }


def boot(**overrides: Any) -> BootstrapFile:
    return BootstrapFile.model_validate(
        {
            "workspaces": [
                {
                    "id": WS,
                    "agents": [{"id": AGENT, "workload": WORKLOAD.name}],
                    "policy": policy(**overrides),
                }
            ]
        }
    )


BOOT = boot()
PUT = {"key": "k", "value": "v"}


def scenario(name: str, *turns: Sequence[tuple[str, dict[str, Any]]]) -> str:
    lines = [f"name: {name}", "turns:"]
    for turn in turns:
        calls = [{"tool": tool, "arguments": arguments} for tool, arguments in turn]
        lines.append(f"  - tool_calls: {json.dumps(calls)}")
    lines.append(f"  - final: {name} done")
    return "\n".join(lines) + "\n"


SCENARIOS = {name: scenario(name, [(name, PUT)]) for name in (t.name for t in TOOLS)}


class RecordingProvider:
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


# --- fixtures and helpers -----------------------------------------------------------------------


def _settings(db: Any, tmp_path: Path) -> Settings:
    bootstrap_file = tmp_path / "bootstrap.yaml"
    bootstrap_file.write_text(yaml.safe_dump(BOOT.model_dump(mode="json")), encoding="utf-8")
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=bootstrap_file,
        scenarios_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def fresh_target(monkeypatch: pytest.MonkeyPatch) -> None:
    TARGET.reset()
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
    monkeypatch.setattr(backoff, "verify_backoff_seconds", lambda declared, attempt, rng=None: 0.0)


@pytest_asyncio.fixture
async def runtime(seeded: Any, tmp_path: Path) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    for name, text in SCENARIOS.items():
        (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    provider = RecordingProvider(ScriptedProvider(tmp_path, env="test"))
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path), bootstrap=BOOT, provider=provider
    )
    runtime.registries[WORKLOAD.name] = ToolRegistry(TEST_WORKLOAD)
    try:
        yield runtime
    finally:
        await runtime.close()


async def call_api(
    seeded: Any, tmp_path: Path, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def submit(
    seeded: Any, tmp_path: Path, name: str, roles: Sequence[str] = ("operator",)
) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": AGENT,
            "requester": {"sub": "alice", "roles": list(roles)},
        },
        "task_input": {"message": f"kv #scenario={name}"},
    }
    response = await call_api(seeded, tmp_path, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def approve(seeded: Any, tmp_path: Path, approval_id: Any) -> None:
    body = {"dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]}}
    url = f"/v1/approvals/{approval_id}/approve"
    response = await call_api(seeded, tmp_path, "POST", url, json=body)
    assert response.status_code == 200, response.text


async def cancel(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> None:
    body = {"dev_identity": {"workspace_id": WS, "sub": "alice", "roles": ["operator"]}}
    url = f"/v1/tasks/runs/{run_id}/cancel"
    response = await call_api(seeded, tmp_path, "POST", url, json=body)
    assert response.status_code == 202, response.text


async def reconcile(seeded: Any, tmp_path: Path, entry_id: Any, outcome: str) -> httpx.Response:
    body = {
        "dev_identity": {"workspace_id": WS, "sub": "dave", "roles": ["reconciler"]},
        "outcome": outcome,
        "justification": "checked the target",
    }
    url = f"/v1/ops/ledger/{entry_id}/reconcile"
    return await call_api(seeded, tmp_path, "POST", url, json=body)


async def view(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> dict[str, Any]:
    response = await call_api(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def drain(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> None:
    for _ in range(120):
        if not await process_next_job(runtime, kinds):
            return
    raise AssertionError("the queue did not drain")


def sql(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_status(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return sql(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


@dataclass(frozen=True)
class Entry:
    id: Any
    state: str
    resolution: str | None
    resolved_by: str | None
    verification_attempts: int
    evidence: dict[str, Any] | None
    idempotency_key: str
    attempts: int


def entry(seeded: Any, run_id: uuid.UUID) -> Entry:
    [row] = sql(
        seeded,
        "SELECT id, state, resolution, resolved_by, verification_attempts,"
        " verification_evidence_masked, idempotency_key, attempts FROM tether.ledger_entries"
        " WHERE run_id = %s",
        run_id,
    )
    return Entry(*row)


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = sql(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event_type for (event_type,) in found]


def verification_events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    return [e for e in events(seeded, run_id) if e.startswith("VERIF")]


def payload_of(seeded: Any, run_id: uuid.UUID, event_type: str) -> list[dict[str, Any]]:
    found = sql(
        seeded,
        "SELECT payload_masked FROM tether.audit_events WHERE run_id = %s AND event_type = %s"
        " ORDER BY seq",
        run_id,
        event_type,
    )
    return [dict(p) for (p,) in found]


def exhausted_reason(seeded: Any, run_id: uuid.UUID) -> str:
    [found] = payload_of(seeded, run_id, "VERIFICATION_EXHAUSTED")
    return str(found["reason"])


def verify_jobs(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return sql(
        seeded,
        "SELECT state, payload FROM tether.jobs WHERE run_id = %s AND kind = 'verify_outcome'",
        run_id,
    )


def tool_results(runtime: WorkerRuntime) -> list[tuple[str | None, Any]]:
    provider = runtime.provider
    assert isinstance(provider, RecordingProvider)
    return [(m.outcome, m.content) for m in provider.calls[-1] if m.role == "tool"]


def model_calls(runtime: WorkerRuntime) -> int:
    provider = runtime.provider
    assert isinstance(provider, RecordingProvider)
    return len(provider.calls)


def all_text(seeded: Any) -> str:
    """Every persisted row a sink could expose: ledger, audit, jobs-free views."""
    rows = sql(seeded, "SELECT row_to_json(e)::text FROM tether.ledger_entries e")
    rows += sql(seeded, "SELECT row_to_json(a)::text FROM tether.audit_events a")
    return "\n".join(text for (text,) in rows)


async def until_indeterminate(runtime: WorkerRuntime) -> None:
    """Everything up to the unknown outcome; the ``verify_outcome`` job is left queued."""
    await drain(runtime, NOT_VERIFY)


# --- definitive results -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t27_a_write_that_applied_is_verified_applied_and_the_run_goes_on(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await drain(runtime)
    found = entry(seeded, run_id)
    assert (found.state, found.resolution, found.resolved_by) == (
        "completed",
        "verified_applied",
        "system:verification",
    )
    assert found.verification_attempts == 1
    assert found.evidence == {"key": "k", "present": True, "receipt": MASK}
    assert verification_events(seeded, run_id) == ["VERIFICATION_ATTEMPTED", "VERIFIED"]
    [(outcome, content)] = tool_results(runtime)
    assert outcome == "applied"
    assert json.loads(content) == {
        "resolution": "verified_applied",
        "verified": True,
        "evidence": {"key": "k", "present": True, "receipt": MASK},
    }
    assert run_status(seeded, run_id)[0] == "succeeded"
    assert TARGET.sends == 1  # the write was never re-sent
    assert TARGET.mutations == 1
    assert CANARY not in all_text(seeded)
    assert (await view(seeded, tmp_path, run_id))["audit_chain_valid"] is True


@pytest.mark.asyncio
async def test_t27_a_write_that_did_not_apply_is_verified_not_applied(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.writes.append("error")
    run_id = await submit(seeded, tmp_path, "kv_put")
    await drain(runtime)
    found = entry(seeded, run_id)
    assert (found.state, found.resolution) == ("failed", "verified_not_applied")
    [(outcome, content)] = tool_results(runtime)
    assert outcome == "not_applied"
    assert json.loads(content)["verified"] is True
    assert run_status(seeded, run_id)[0] == "succeeded"
    assert TARGET.sends == 1
    assert TARGET.mutations == 0
    [verified] = payload_of(seeded, run_id, "VERIFIED")
    assert verified["resolution"] == "verified_not_applied"


@pytest.mark.asyncio
async def test_t27_an_approval_gated_write_is_verified_without_a_second_approval(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put_gated")
    await drain(runtime)
    [(approval_id,)] = sql(seeded, "SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
    await approve(seeded, tmp_path, approval_id)
    await drain(runtime)
    assert entry(seeded, run_id).resolution == "verified_applied"
    assert sql(seeded, "SELECT count(*) FROM tether.approvals WHERE run_id = %s", run_id) == [(1,)]
    assert events(seeded, run_id).count("APPROVAL_REQUESTED") == 1
    assert run_status(seeded, run_id)[0] == "succeeded"


@pytest.mark.asyncio
async def test_t27_the_probe_receives_only_the_original_arguments_and_key(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await drain(runtime)
    [(args, ctx)] = TARGET.probe_calls
    assert args == PutIn(**PUT)
    assert ctx == VerifyContext(idempotency_key=entry(seeded, run_id).idempotency_key)


@pytest.mark.asyncio
async def test_t27_other_arguments_in_the_job_are_refused_before_any_probe(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    sql(
        seeded,
        "UPDATE tether.jobs SET payload = %s WHERE run_id = %s AND kind = 'verify_outcome'"
        " RETURNING id",
        json.dumps({"arguments": {"key": "other", "value": "v"}}),
        run_id,
    )
    await drain(runtime)
    assert TARGET.probe_calls == []
    assert exhausted_reason(seeded, run_id) == "policy_denied"
    [ended] = payload_of(seeded, run_id, "VERIFICATION_EXHAUSTED")
    assert ended["policy_reasons"] == ["target_mismatch"]
    assert entry(seeded, run_id).state == "indeterminate"
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


# --- no definitive result: human reconciliation -------------------------------------------------


@pytest.mark.asyncio
async def test_t27_ambiguous_probes_exhaust_to_reconciliation(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.probes.extend(["inconclusive"] * 3)
    run_id = await submit(seeded, tmp_path, "kv_put")
    await drain(runtime)
    found = entry(seeded, run_id)
    assert (found.state, found.resolution, found.verification_attempts) == (
        "indeterminate",
        None,
        3,
    )
    assert verification_events(seeded, run_id) == [
        *["VERIFICATION_ATTEMPTED", "VERIFICATION_INCONCLUSIVE"] * 3,
        "VERIFICATION_EXHAUSTED",
    ]
    assert exhausted_reason(seeded, run_id) == "attempts_exhausted"
    assert all(
        p["evidence"]["receipt"] == MASK
        for p in payload_of(seeded, run_id, "VERIFICATION_INCONCLUSIVE")
    )
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    assert TARGET.sends == 1
    response = await reconcile(seeded, tmp_path, found.id, "applied")
    assert response.status_code == 200, response.text
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "succeeded"


@pytest.mark.asyncio
async def test_t27_probe_errors_and_malformed_answers_exhaust_the_attempts(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.probes.extend(["error", "invalid", "error"])
    run_id = await submit(seeded, tmp_path, "kv_put")
    await drain(runtime)
    reasons = [p["reason"] for p in payload_of(seeded, run_id, "VERIFICATION_INCONCLUSIVE")]
    assert reasons == ["probe_error:ConnectError", "evidence_invalid", "probe_error:ConnectError"]
    assert exhausted_reason(seeded, run_id) == "attempts_exhausted"
    assert entry(seeded, run_id).state == "indeterminate"


@pytest.mark.asyncio
async def test_t27_a_probe_timeout_is_inconclusive(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    TARGET.probes.extend(["slow", "slow", "slow"])
    run_id = await submit(seeded, tmp_path, "kv_put")
    await drain(runtime)
    reasons = [p["reason"] for p in payload_of(seeded, run_id, "VERIFICATION_INCONCLUSIVE")]
    assert reasons == ["probe_timeout"] * 3
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_t27_a_probe_denied_by_policy_is_never_run(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put", roles=("writer",))
    await drain(runtime)
    assert TARGET.probe_calls == []
    assert verification_events(seeded, run_id) == ["VERIFICATION_EXHAUSTED"]
    [ended] = payload_of(seeded, run_id, "VERIFICATION_EXHAUSTED")
    assert ended["reason"] == "policy_denied"
    assert ended["policy_reasons"] == ["requester_lacks:kv:read"]
    assert entry(seeded, run_id).state == "indeterminate"
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_t27_a_policy_changed_since_the_write_refuses_the_probe(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    # A deployment that grants more: the probe would be allowed under the new version alone.
    changed = boot(role_permissions={"operator": ["kv:read", "kv:write", "kv:admin"]})
    apply_bootstrap(seeded.db.migrator_dsn, changed)
    runtime.bootstrap = changed
    await drain(runtime)
    assert TARGET.probe_calls == []
    [ended] = payload_of(seeded, run_id, "VERIFICATION_EXHAUSTED")
    assert (ended["reason"], ended["policy_reasons"]) == ("policy_denied", ["policy_changed"])


@pytest.mark.asyncio
async def test_t27_a_stale_policy_in_this_process_refuses_the_probe(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    apply_bootstrap(seeded.db.migrator_dsn, boot(agent_permissions={AGENT: ["kv:write"]}))
    await drain(runtime)
    assert TARGET.probe_calls == []
    assert exhausted_reason(seeded, run_id) == "policy_stale"


@pytest.mark.asyncio
async def test_t27_the_pinned_version_gone_refuses_the_probe(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    newer = kv_tool("kv_put", version="2.0.0")
    runtime.registries[WORKLOAD.name] = ToolRegistry(
        workload(newer, KV_PUT_GATED, KV_PUT_PLAIN, KV_PUT_IDEM)
    )
    await drain(runtime)
    assert TARGET.probe_calls == []
    assert exhausted_reason(seeded, run_id) == "tool_version_unavailable"


@pytest.mark.asyncio
async def test_t27_a_tool_without_a_declaration_is_never_verified(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put_plain")
    await drain(runtime)
    assert verify_jobs(seeded, run_id) == []
    assert verification_events(seeded, run_id) == []
    assert entry(seeded, run_id).state == "indeterminate"
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_t27_an_active_stop_skips_the_probe(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    await cancel(seeded, tmp_path, run_id)
    await drain(runtime)
    assert TARGET.probe_calls == []
    assert verification_events(seeded, run_id) == [
        "VERIFICATION_SKIPPED",
        "VERIFICATION_EXHAUSTED",
    ]
    assert [p["reason"] for p in payload_of(seeded, run_id, "VERIFICATION_SKIPPED")] == ["stopped"]
    assert exhausted_reason(seeded, run_id) == "stopped"
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    response = await reconcile(seeded, tmp_path, entry(seeded, run_id).id, "applied")
    assert response.status_code == 200, response.text
    await drain(runtime)
    status, summary = run_status(seeded, run_id)
    assert status == "cancelled"
    assert "kv_put reconciled_applied" in summary


@pytest.mark.asyncio
async def test_t27_a_stop_during_a_probe_still_records_its_definitive_result(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    turns = model_calls(runtime)

    async def stop_now() -> None:
        await cancel(seeded, tmp_path, run_id)

    TARGET.during_probe = stop_now
    await drain(runtime)
    assert entry(seeded, run_id).resolution == "verified_applied"
    status, summary = run_status(seeded, run_id)
    assert status == "cancelled"
    assert "kv_put verified_applied" in summary
    assert model_calls(runtime) == turns  # a verified outcome after a stop only terminates


@pytest.mark.asyncio
async def test_t27_exhausted_job_deliveries_end_verification(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    sql(
        seeded,
        "UPDATE tether.jobs SET attempts = max_attempts WHERE run_id = %s"
        " AND kind = 'verify_outcome' RETURNING id",
        run_id,
    )
    await drain(runtime)
    assert TARGET.probe_calls == []
    assert exhausted_reason(seeded, run_id) == "delivery_exhausted"
    assert [state for state, _ in verify_jobs(seeded, run_id)] == ["dead"]


# --- the human wins -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t27_a_human_reconciliation_before_the_probe_means_no_probe(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    response = await reconcile(seeded, tmp_path, entry(seeded, run_id).id, "not_applied")
    assert response.status_code == 200, response.text
    await drain(runtime)
    assert TARGET.probe_calls == []
    assert verification_events(seeded, run_id) == []
    assert entry(seeded, run_id).resolution == "reconciled_not_applied"


@pytest.mark.asyncio
async def test_t27_a_late_probe_after_human_reconciliation_is_evidence_only(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    entry_id = entry(seeded, run_id).id

    async def human_first() -> None:
        response = await reconcile(seeded, tmp_path, entry_id, "not_applied")
        assert response.status_code == 200, response.text

    TARGET.during_probe = human_first
    await drain(runtime)
    found = entry(seeded, run_id)
    # The probe saw the write applied; the human said not applied first, and the human wins.
    assert (found.state, found.resolution, found.evidence) == (
        "failed",
        "reconciled_not_applied",
        None,
    )
    assert verification_events(seeded, run_id) == ["VERIFICATION_ATTEMPTED", "VERIFICATION_LATE"]
    [late] = payload_of(seeded, run_id, "VERIFICATION_LATE")
    assert (late["result"], late["resolution"]) == ("confirmed_applied", "reconciled_not_applied")
    assert late["evidence"]["receipt"] == MASK
    [(outcome, _)] = tool_results(runtime)
    assert outcome == "not_applied"


@pytest.mark.asyncio
async def test_t27_a_human_reconciliation_after_verification_is_a_conflict(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await drain(runtime)
    response = await reconcile(seeded, tmp_path, entry(seeded, run_id).id, "applied")
    assert response.status_code == 409
    assert entry(seeded, run_id).resolution == "verified_applied"


# --- inspection and M5 ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t27_inspection_shows_verification_in_progress_and_its_attempts(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(seeded, tmp_path, "kv_put")
    await until_indeterminate(runtime)
    [ledger] = (await view(seeded, tmp_path, run_id))["ledger"]
    assert (ledger["state"], ledger["verification"], ledger["verification_attempts"]) == (
        "indeterminate",
        "in_progress",
        0,
    )
    await drain(runtime)
    [ledger] = (await view(seeded, tmp_path, run_id))["ledger"]
    assert ledger["verification"] is None
    assert (ledger["resolution"], ledger["verification_attempts"]) == ("verified_applied", 1)
    assert ledger["verification_evidence"] == {"key": "k", "present": True, "receipt": MASK}


@pytest.mark.asyncio
async def test_m5_a_dispatched_entry_with_other_arguments_is_indeterminate_and_never_re_sent(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """An idempotent write whose first send was unknown is due for a re-send, but the job now
    carries arguments that are not the governed call's: never re-sent, the outcome is unknown
    (``indeterminate``), the run is woken, and verification refuses the other arguments."""
    TARGET.writes.extend(["error"])
    run_id = await submit(seeded, tmp_path, "kv_put_idem")
    # Up to the first (failed) send; its retry stays queued.
    for _ in range(10):
        states = sql(seeded, "SELECT state FROM tether.ledger_entries WHERE run_id = %s", run_id)
        if states == [("dispatched",)]:
            break
        await process_next_job(runtime, NOT_VERIFY)
    assert entry(seeded, run_id).state == "dispatched"
    sql(
        seeded,
        "UPDATE tether.jobs SET payload = %s, run_after = clock_timestamp()"
        " WHERE run_id = %s AND kind = 'execute_tool' AND state = 'ready' RETURNING id",
        json.dumps({"arguments": {"key": "other", "value": "v"}}),
        run_id,
    )
    await drain(runtime)
    assert TARGET.sends == 1
    found = entry(seeded, run_id)
    assert found.state == "indeterminate"
    [(outcome_class, reason)] = sql(
        seeded,
        "SELECT outcome_class, outcome_reason FROM tether.ledger_entries WHERE run_id = %s",
        run_id,
    )
    assert (outcome_class, reason) == (
        "unknown",
        "outcome unknown: call hash mismatch; not re-sent",
    )
    assert TARGET.probe_calls == []
    [ended] = payload_of(seeded, run_id, "VERIFICATION_EXHAUSTED")
    assert ended["policy_reasons"] == ["target_mismatch"]
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
