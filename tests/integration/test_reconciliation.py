"""T-17 / T-25: ambiguous writes become ``indeterminate`` and only a human resolves them.

Covers ADR-0007 (honest outcomes, human-only resolution), ADR-0008 (no re-send of a
non-idempotent write), TX8 (reconcile: ledger resolution + ``RECONCILED`` + wake, one
transaction) and the resolution routing in the run graph: ``applied`` continues,
``not_applied`` gives a definite result, ``abandon`` terminates the run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import yaml
from pydantic import BaseModel, SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.core.statuses import JobKind
from tether.policy.config import BootstrapFile
from tether.providers.history import ModelTurn, NeutralMessage
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS = "ws-r"
OTHER_WS = "ws-r2"
POLICY = {
    "rules": [
        {"tool": "get_service_health", "decision": "allow", "requester_roles": ["operator"]},
        {"tool": "probe_write", "decision": "allow", "requester_roles": ["operator"]},
    ],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
}
BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": "agent-r", "workload": "incident_remediation"}],
                "policy": POLICY,
            },
            {
                "id": OTHER_WS,
                "agents": [{"id": "agent-r", "workload": "incident_remediation"}],
                "policy": POLICY,
            },
        ]
    }
)

SCENARIOS = {
    "probe": (
        "name: probe\nturns:\n"
        "  - tool_calls: [{tool: probe_write, arguments: {target: a}}]\n"
        "  - final: probed\n"
    ),
    "probe_twice": (
        "name: probe_twice\nturns:\n"
        "  - tool_calls: [{tool: probe_write, arguments: {target: a}}]\n"
        "  - tool_calls: [{tool: probe_write, arguments: {target: a}}]\n"
        "  - final: probed again\n"
    ),
    "probe_then_read": (
        "name: probe_then_read\nturns:\n"
        "  - tool_calls:\n"
        "      - {tool: probe_write, arguments: {target: a}}\n"
        "      - {tool: get_service_health, arguments: {service: checkout}}\n"
        "  - final: done\n"
    ),
}


# --- a test-only write tool with no idempotency support (PRD F7) ---------------------------


class ProbeIn(BaseModel):
    target: str


class ProbeOut(BaseModel):
    target: str
    applied: bool


@dataclass
class Probe:
    """Counts real sends. ``script`` says what each send does: ``drop`` = applied, then the
    connection drops before any response (an unknown outcome); ``ok`` = applied and answered."""

    script: list[str] = field(default_factory=list)
    sends: int = 0

    async def handler(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        self.sends += 1
        if (self.script.pop(0) if self.script else "ok") == "drop":
            raise httpx.RemoteProtocolError("connection dropped after the request was sent")
        return ProbeOut(target=args.target, applied=True)


PROBE = Probe()
PROBE_TOOL = ToolDescriptor(
    name="probe_write",
    version="1.0.0",
    description="Test-only write without idempotency support.",
    input_model=ProbeIn,
    output_model=ProbeOut,
    effect="write",
    idempotency="unsupported",
    handler=PROBE.handler,
)
TEST_WORKLOAD = Workload(
    name=WORKLOAD.name,
    tools=(*WORKLOAD.tools, PROBE_TOOL),
    agent=AgentDefinition(system_prompt="test", tools=(*WORKLOAD.agent.tools, PROBE_TOOL.name)),
)


class RecordingProvider:
    """Wraps the scripted provider and keeps the history of every model call."""

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


def tool_results(provider: RecordingProvider) -> list[tuple[str | None, str]]:
    last = provider.calls[-1]
    return [(m.outcome, m.content) for m in last if m.role == "tool"]


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
    PROBE.script.clear()
    PROBE.sends = 0
    yield
    demo_client.set_transport(None)


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
    seeded: Any, tmp_path: Path, method: str, url: str, env: str = "test", **kwargs: Any
) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path, env=env))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def submit(seeded: Any, tmp_path: Path, scenario: str) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": "agent-r",
            "requester": {"sub": "alice", "roles": ["operator", "reconciler"]},
        },
        "task_input": {"message": f"probe #scenario={scenario}"},
    }
    response = await call_api(seeded, tmp_path, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def reconcile(
    seeded: Any,
    tmp_path: Path,
    ledger_id: Any,
    outcome: str,
    *,
    sub: str = "dave",
    roles: Sequence[str] = ("reconciler",),
    workspace: str = WS,
    justification: str | None = "checked the target by hand",
    env: str = "test",
) -> httpx.Response:
    body: dict[str, Any] = {
        "dev_identity": {"workspace_id": workspace, "sub": sub, "roles": list(roles)},
        "outcome": outcome,
    }
    if justification is not None:
        body["justification"] = justification
    return await call_api(
        seeded, tmp_path, "POST", f"/v1/ops/ledger/{ledger_id}/reconcile", env=env, json=body
    )


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(50):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_status(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return rows(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


def ledger(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return rows(
        seeded,
        "SELECT id, step_ordinal, tool, state, attempts, resolution, resolved_by, justification,"
        " idempotency_key FROM tether.ledger_entries WHERE run_id = %s ORDER BY step_ordinal",
        run_id,
    )


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event_type for (event_type,) in found]


def wakes(seeded: Any, run_id: uuid.UUID) -> int:
    return rows(
        seeded,
        "SELECT count(*) FROM tether.jobs WHERE run_id = %s AND kind = 'advance_run'",
        run_id,
    )[0][0]


def model_calls(seeded: Any, run_id: uuid.UUID) -> int:
    return rows(seeded, "SELECT count(*) FROM tether.model_calls WHERE run_id = %s", run_id)[0][0]


async def chain_valid(seeded: Any, tmp_path: Path, run_id: uuid.UUID) -> bool:
    response = await call_api(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    assert response.status_code == 200, response.text
    return bool(response.json()["audit_chain_valid"])


async def indeterminate_run(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, scenario: str = "probe"
) -> tuple[uuid.UUID, uuid.UUID]:
    PROBE.script[:] = ["drop"]
    run_id = await submit(seeded, tmp_path, scenario)
    await drain(runtime)
    [first, *_] = ledger(seeded, run_id)
    assert first[3] == "indeterminate"
    return run_id, first[0]


# --- the ambiguous write is never retried and the run waits ---------------------------------


@pytest.mark.asyncio
async def test_t17_an_unknown_write_outcome_becomes_indeterminate_and_is_never_retried(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, _ = await indeterminate_run(runtime, seeded, tmp_path)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    [(_, ordinal, tool, state, attempts, resolution, *_)] = ledger(seeded, run_id)
    assert (ordinal, tool, state, attempts, resolution) == (
        0,
        "probe_write",
        "indeterminate",
        1,
        None,
    )
    assert PROBE.sends == 1
    # More worker passes change nothing: no retry, no model call, the run keeps waiting.
    await drain(runtime)
    assert PROBE.sends == 1
    assert model_calls(seeded, run_id) == 1
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    assert events(seeded, run_id)[-2:] == ["DISPATCHED", "TOOL_OUTCOME"]
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t17_a_redelivered_non_idempotent_write_is_not_re_sent(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    """Crash after TX6: the entry is ``dispatched`` with no outcome, and the job comes back."""
    run_id = await submit(seeded, tmp_path, "probe")
    await process_next_job(runtime, kinds=(JobKind.ADVANCE_RUN,))
    rows(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'dispatched', attempts = 1"
        " WHERE run_id = %s RETURNING id",
        run_id,
    )
    await drain(runtime)
    assert PROBE.sends == 0
    [(_, _, _, state, attempts, *_)] = ledger(seeded, run_id)
    assert (state, attempts) == ("indeterminate", 1)
    assert "REDISPATCHED" not in events(seeded, run_id)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


# --- resolution routing ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t25_applied_records_the_resolution_and_continues_without_re_running_the_tool(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path)
    response = await reconcile(seeded, tmp_path, ledger_id, "applied")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ledger_id": str(ledger_id),
        "state": "completed",
        "resolution": "reconciled_applied",
    }
    [(_, _, _, state, _, resolution, by, why, _)] = ledger(seeded, run_id)
    assert (state, resolution, by, why) == (
        "completed",
        "reconciled_applied",
        "dave",
        "checked the target by hand",
    )
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "probed")
    assert PROBE.sends == 1
    assert tool_results(runtime.provider) == [  # type: ignore[arg-type]
        ("applied", '{"resolution": "reconciled_applied"}')
    ]
    trail = events(seeded, run_id)
    assert trail[trail.index("TOOL_OUTCOME") + 1] == "RECONCILED"
    assert trail[-1] == "RUN_FINISHED"
    assert await chain_valid(seeded, tmp_path, run_id)
    view = await call_api(
        seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    [entry] = view.json()["ledger"]
    assert (entry["state"], entry["resolution"], entry["resolved_by"]) == (
        "completed",
        "reconciled_applied",
        "dave",
    )
    reconciled = [e for e in view.json()["audit"] if e["event_type"] == "RECONCILED"]
    assert [(e["actor"], e["tool"]) for e in reconciled] == [("dave", "probe_write")]


@pytest.mark.asyncio
async def test_t25_not_applied_is_definite_and_a_re_proposal_is_a_new_execution(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path, "probe_twice")
    response = await reconcile(seeded, tmp_path, ledger_id, "not_applied")
    assert response.status_code == 200, response.text
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "probed again")
    first, second = ledger(seeded, run_id)
    assert first[3:6] == ("failed", 1, "reconciled_not_applied")
    assert second[1:6] == (1, "probe_write", "completed", 1, None)
    assert first[8] != second[8]  # a new idempotency key: a new logical execution
    assert PROBE.sends == 2  # the original send, then the new execution; never a re-run
    outcomes = [outcome for outcome, _ in tool_results(runtime.provider)]  # type: ignore[arg-type]
    assert outcomes == ["not_applied", "applied"]
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t25_abandon_terminates_the_run_with_no_further_model_call(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path)
    response = await reconcile(seeded, tmp_path, ledger_id, "abandon")
    assert response.status_code == 200, response.text
    await drain(runtime)
    status, summary = run_status(seeded, run_id)
    assert status == "abandoned"
    assert summary is not None
    assert "abandon" in summary
    [(_, _, _, state, _, resolution, *_)] = ledger(seeded, run_id)
    assert (state, resolution) == ("abandoned", "reconciled_abandon")
    assert model_calls(seeded, run_id) == 1
    assert PROBE.sends == 1
    assert events(seeded, run_id)[-2:] == ["RECONCILED", "RUN_FINISHED"]
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
async def test_t25_a_reconciled_applied_call_lets_the_rest_of_the_turn_continue(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path, "probe_then_read")
    assert (await reconcile(seeded, tmp_path, ledger_id, "applied")).status_code == 200
    await drain(runtime)
    assert [entry[2:4] for entry in ledger(seeded, run_id)] == [
        ("probe_write", "completed"),
        ("get_service_health", "completed"),
    ]
    assert run_status(seeded, run_id) == ("succeeded", "done")


@pytest.mark.asyncio
async def test_t25_a_reconciled_not_applied_call_skips_the_rest_of_the_turn(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path, "probe_then_read")
    assert (await reconcile(seeded, tmp_path, ledger_id, "not_applied")).status_code == 200
    await drain(runtime)
    assert [entry[2] for entry in ledger(seeded, run_id)] == ["probe_write"]
    outcomes = [outcome for outcome, _ in tool_results(runtime.provider)]  # type: ignore[arg-type]
    assert outcomes == ["not_applied", "skipped"]
    assert run_status(seeded, run_id) == ("succeeded", "done")


# --- TX8: idempotent, eligible, workspace-scoped, current policy ---------------------------


@pytest.mark.asyncio
async def test_t25_repeating_the_same_reconciliation_is_a_no_op(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path)
    first = await reconcile(seeded, tmp_path, ledger_id, "applied")
    wakes_after_first = wakes(seeded, run_id)
    again = await reconcile(seeded, tmp_path, ledger_id, "applied", justification="retry")
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert events(seeded, run_id).count("RECONCILED") == 1
    assert wakes(seeded, run_id) == wakes_after_first
    [entry] = ledger(seeded, run_id)
    assert entry[7] == "checked the target by hand"
    changed = await reconcile(seeded, tmp_path, ledger_id, "abandon")
    assert changed.status_code == 409
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "succeeded"
    assert PROBE.sends == 1
    assert await chain_valid(seeded, tmp_path, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sub", "roles", "workspace"),
    [
        ("alice", ("reconciler",), WS),  # the requester: separation of duties
        ("bob", ("oncall",), WS),  # no reconciler role
        ("dave", ("reconciler",), OTHER_WS),  # another workspace
    ],
)
async def test_t25_ineligible_callers_get_not_found_and_change_nothing(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    sub: str,
    roles: tuple[str, ...],
    workspace: str,
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path)
    response = await reconcile(
        seeded, tmp_path, ledger_id, "applied", sub=sub, roles=roles, workspace=workspace
    )
    assert response.status_code == 404
    assert ledger(seeded, run_id)[0][3:6] == ("indeterminate", 1, None)
    assert "RECONCILED" not in events(seeded, run_id)


@pytest.mark.asyncio
async def test_t25_unknown_entries_and_resolved_automatic_entries_are_refused(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    missing = await reconcile(seeded, tmp_path, uuid.uuid4(), "applied")
    assert missing.status_code == 404
    PROBE.script[:] = ["ok"]
    run_id = await submit(seeded, tmp_path, "probe")
    await drain(runtime)
    [entry] = ledger(seeded, run_id)
    assert entry[3] == "completed"
    response = await reconcile(seeded, tmp_path, entry[0], "not_applied")
    assert response.status_code == 409
    assert ledger(seeded, run_id)[0][5] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "justification"),
    [("applied", None), ("applied", ""), ("maybe", "looks fine")],
)
async def test_t25_a_justification_and_a_known_outcome_are_required(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    outcome: str,
    justification: str | None,
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path)
    response = await reconcile(seeded, tmp_path, ledger_id, outcome, justification=justification)
    assert response.status_code == 422
    assert ledger(seeded, run_id)[0][5] is None


@pytest.mark.asyncio
async def test_t25_no_reconciliation_is_decided_under_a_stale_policy_version(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path)
    rows(
        seeded,
        "UPDATE tether.policy_versions SET active = false WHERE workspace_id = %s RETURNING 1",
        WS,
    )
    response = await reconcile(seeded, tmp_path, ledger_id, "applied")
    assert response.status_code == 503
    assert ledger(seeded, run_id)[0][5] is None


@pytest.mark.asyncio
async def test_t25_the_dev_reconciler_shortcut_is_refused_in_prod(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, ledger_id = await indeterminate_run(runtime, seeded, tmp_path)
    response = await reconcile(seeded, tmp_path, ledger_id, "applied", env="prod")
    assert response.status_code == 404
    assert ledger(seeded, run_id)[0][5] is None
