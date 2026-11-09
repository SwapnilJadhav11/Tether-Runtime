"""S1.9 / S7.3 (T-16 core cases): one trace per run across the api, the queue and the worker;
the important spans; metrics for representative paths; nothing secret or sensitive in logs,
spans or metric labels; and telemetry that never breaks execution (ADR-0015, ADR-0017).

Spans and metrics go to the in-memory exporters from ``tests/conftest.py``; the code under test
uses the global OpenTelemetry API exactly as the api and worker processes do.
"""

from __future__ import annotations

import io
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
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
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, SecretStr
from services.demo_target_service.app import create_app as create_demo_target
from workloads.incident_remediation import WORKLOAD
from workloads.incident_remediation import client as demo_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.identity import api_keys
from tether.observability import metrics, tracing
from tether.policy.config import BootstrapFile
from tether.queue import backoff
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.settings import Settings
from tether.storage.engine import create_database_engine
from tether.storage.uow import UnitOfWork
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

WS = "ws-a"
TASKS, APPROVALS, OPS = "tether-tasks", "tether-approvals", "tether-ops"
SIGNERS = {aud: ed25519.Ed25519PrivateKey.generate() for aud in (TASKS, APPROVALS, OPS)}
APP_KEY = api_keys.generate("app-o")
TASK_MESSAGE = "obs-task-message"
Delta = Callable[[], dict[Any, float]]


def _public_pem(key: ed25519.Ed25519PrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


BOOT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                "id": WS,
                "agents": [{"id": "agent-1", "workload": WORKLOAD.name}],
                "issuers": [
                    {
                        "id": aud,
                        "iss": f"https://issuer.test/{aud}",
                        "audiences": [aud],
                        "algorithm": "EdDSA",
                        "public_keys": {f"{aud}-1": _public_pem(key)},
                    }
                    for aud, key in SIGNERS.items()
                ],
                "api_keys": [
                    {
                        "key_id": APP_KEY.key_id,
                        "salt": APP_KEY.salt.hex(),
                        "secret_hash": APP_KEY.secret_hash.hex(),
                        "agents": ["agent-1"],
                    }
                ],
                "policy": {
                    "rules": [
                        {"tool": "get_service_health", "decision": "allow"},
                        {
                            "tool": "restart_service",
                            "decision": "require_approval",
                            "approver_roles": ["approver"],
                        },
                        {"tool": "probe_flaky", "decision": "allow"},
                    ],
                    "reconciliation": {"reconciler_roles": ["sre"]},
                },
            }
        ]
    }
)
SCENARIOS = {
    "remediate": (
        "name: remediate\nturns:\n"
        "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
        "  - tool_calls: [{tool: restart_service, arguments: {service: checkout}}]\n"
        "  - final: restarted\n"
    ),
    "read": (
        "name: read\nturns:\n"
        "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
        "  - final: read done\n"
    ),
    "flaky": (
        "name: flaky\nturns:\n"
        "  - tool_calls: [{tool: probe_flaky, arguments: {target: checkout}}]\n"
        "  - final: flaky done\n"
    ),
}


class ProbeIn(BaseModel):
    target: str


class ProbeOut(BaseModel):
    target: str


async def _never_answers(args: ProbeIn, ctx: ToolContext) -> ProbeOut:
    raise httpx.ReadTimeout("no response")


FLAKY = ToolDescriptor(
    name="probe_flaky",
    version="1.0.0",
    description="Test-only idempotent write whose target never answers.",
    input_model=ProbeIn,
    output_model=ProbeOut,
    effect="write",
    idempotency="supported",
    handler=_never_answers,
)
TEST_WORKLOAD = Workload(
    name=WORKLOAD.name,
    tools=(*WORKLOAD.tools, FLAKY),
    agent=AgentDefinition(system_prompt="test", tools=(*WORKLOAD.agent.tools, FLAKY.name)),
)

# Every credential a test sends, so the containment test can look for each one.
ISSUED: list[str] = []


def token(sub: str, roles: Sequence[str], audience: str) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://issuer.test/{audience}",
        "aud": audience,
        "sub": sub,
        "workspace": WS,
        "roles": list(roles),
        "iat": now,
        "exp": now + 300,
    }
    issued = jwt.encode(
        claims, SIGNERS[audience], algorithm="EdDSA", headers={"kid": f"{audience}-1"}
    )
    ISSUED.append(issued)
    return issued


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
def demo_target(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    demo_client.set_transport(httpx.ASGITransport(app=create_demo_target()))
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
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


async def call(seeded: Any, tmp_path: Path, method: str, url: str, **kwargs: Any) -> httpx.Response:
    app = create_app(_settings(seeded.db, tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


def alice() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token('alice', ['operator'], TASKS)}",
        "X-API-Key": APP_KEY.raw,
    }


async def submit(seeded: Any, tmp_path: Path, scenario: str) -> uuid.UUID:
    response = await call(
        seeded,
        tmp_path,
        "POST",
        "/v1/tasks/runs",
        headers=alice(),
        json={"task_input": {"message": f"{TASK_MESSAGE} #scenario={scenario}"}},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def drain(runtime: WorkerRuntime) -> None:
    for _ in range(80):
        if not await process_next_job(runtime):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def status(seeded: Any, run_id: uuid.UUID) -> str:
    return str(rows(seeded, "SELECT status FROM tether.runs WHERE id = %s", run_id)[0][0])


def run_trace_parent(seeded: Any, run_id: uuid.UUID) -> Any:
    return rows(seeded, "SELECT trace_parent FROM tether.runs WHERE id = %s", run_id)[0][0]


async def remediate(runtime: WorkerRuntime, seeded: Any, tmp_path: Path) -> uuid.UUID:
    """The governed demo flow: read, restart pending approval, approve, restart, finish."""
    run_id = await submit(seeded, tmp_path, "remediate")
    await drain(runtime)
    assert status(seeded, run_id) == "awaiting_approval"
    [(approval_id,)] = rows(seeded, "SELECT id FROM tether.approvals WHERE run_id = %s", run_id)
    decided = await call(
        seeded,
        tmp_path,
        "POST",
        f"/v1/approvals/{approval_id}/approve",
        headers={"Authorization": f"Bearer {token('bob', ['approver'], APPROVALS)}"},
        json={"reason": "restart ok"},
    )
    assert decided.status_code == 200, decided.text
    await drain(runtime)
    assert status(seeded, run_id) == "succeeded"
    return run_id


def trace_of(traceparent: str | None) -> int:
    parsed = tracing.parse_traceparent(traceparent)
    assert parsed is not None, traceparent
    return parsed.trace_id


def named(spans: Sequence[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]


def _scope_name(span: ReadableSpan) -> str:
    return span.instrumentation_scope.name if span.instrumentation_scope else ""


def tether_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    """Spans Tether emits (the in-process demo target, a separate service, emits its own)."""
    return [s for s in exporter.get_finished_spans() if _scope_name(s) == "tether"]


def demo_target_traces(spans: Sequence[ReadableSpan]) -> set[int]:
    """Traces started by the in-process demo target's own requests (not Tether's)."""
    return {
        s.context.trace_id
        for s in spans
        if s.context is not None
        and _scope_name(s) == "fastapi"
        and str(attributes(s).get("http.route", "")).startswith("/services")
    }


def attributes(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def key(name: str, **labels: str) -> tuple[str, frozenset[tuple[str, Any]]]:
    return name, frozenset(labels.items())


def total(changed: dict[Any, float], name: str) -> float:
    return sum(value for (metric, _), value in changed.items() if metric == name)


# --- trace continuity -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_run_is_one_trace_from_the_api_through_jobs_to_the_worker(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    run_id = await remediate(runtime, seeded, tmp_path)
    run_trace = trace_of(run_trace_parent(seeded, run_id))
    finished = tether_spans(spans)

    # The submission request is the run's root span.
    [root] = named(finished, "POST /v1/tasks/runs")
    assert root.context is not None
    assert root.parent is None
    assert root.context.trace_id == run_trace

    # Every job of the run carries trace context, and every job segment joins the run's trace
    # as a child of the root, linked to the span that enqueued it (unless that is the root).
    enqueuers = dict(
        rows(seeded, "SELECT id::text, trace_parent FROM tether.jobs WHERE run_id = %s", run_id)
    )
    assert enqueuers
    assert all(trace_of(parent) == run_trace for parent in enqueuers.values())
    segments = [s for s in finished if s.name.startswith("job ")]
    assert {s.name for s in segments} >= {"job advance_run", "job execute_tool"}
    linked = 0
    for segment in segments:
        assert segment.context is not None
        assert segment.parent is not None
        assert segment.context.trace_id == run_trace
        assert segment.parent.span_id == root.context.span_id
        enqueuer = tracing.parse_traceparent(enqueuers[attributes(segment)["tether.job.id"]])
        assert enqueuer is not None
        if enqueuer.span_id == root.context.span_id:
            assert not segment.links  # enqueued by the root itself: the parent says it all
        else:
            assert [link.context.span_id for link in segment.links] == [enqueuer.span_id]
            linked += 1
    assert linked >= 4  # wakes from tool outcomes, the approval decision, and tool executions

    # The human decision is part of the run's trace too, linked to its own request span.
    [decision] = named(finished, "approval.decide")
    assert decision.context is not None
    assert decision.context.trace_id == run_trace
    [approve_request] = named(finished, "POST /v1/approvals/{approval_id}/{action}")
    assert approve_request.context is not None
    assert [link.context.span_id for link in decision.links] == [approve_request.context.span_id]

    # Only the approval request itself is its own trace; everything else is the run's.
    others = {s.context.trace_id for s in finished if s is not approve_request and s.context}
    assert others == {run_trace}

    # Audit events carry the run's trace id for correlation (outside the MAC, ADR-0015).
    audit = rows(seeded, "SELECT trace_id FROM tether.audit_events WHERE run_id = %s", run_id)
    assert audit
    assert {trace_id for (trace_id,) in audit} == {f"{run_trace:032x}"}


@pytest.mark.asyncio
async def test_a_redelivered_job_after_a_worker_crash_stays_in_the_run_trace(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    run_id = await submit(seeded, tmp_path, "read")
    # A worker leased the first job and died: its lease expires and another worker takes it.
    rows(
        seeded,
        "UPDATE tether.jobs SET state = 'leased', lease_token = gen_random_uuid(),"
        " lease_expires_at = clock_timestamp() - interval '1 second', attempts = 1"
        " WHERE run_id = %s AND kind = 'advance_run' RETURNING id",
        run_id,
    )
    await drain(runtime)
    assert status(seeded, run_id) == "succeeded"
    segments = [s for s in tether_spans(spans) if s.name.startswith("job ")]
    assert segments
    run_trace = trace_of(run_trace_parent(seeded, run_id))
    assert {s.context.trace_id for s in segments if s.context} == {run_trace}


# --- spans ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_important_spans_are_emitted_with_safe_attributes(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    run_id = await remediate(runtime, seeded, tmp_path)
    finished = tether_spans(spans)
    assert {s.name for s in finished} >= {
        "POST /v1/tasks/runs",
        "POST /v1/approvals/{approval_id}/{action}",
        "job advance_run",
        "job execute_tool",
        "run.drive",
        "model.call",
        "policy.govern",
        "tool.authorize",
        "tool.execute",
        "approval.decide",
    }
    governed = {
        (attributes(s)["tether.tool"], attributes(s)["tether.policy.decision"])
        for s in named(finished, "policy.govern")
    }
    assert governed == {("get_service_health", "allow"), ("restart_service", "require_approval")}
    executed = {
        (attributes(s)["tether.tool"], attributes(s)["tether.tool.outcome"])
        for s in named(finished, "tool.execute")
    }
    assert executed == {("get_service_health", "completed"), ("restart_service", "completed")}
    models = named(finished, "model.call")
    assert len(models) == 3
    assert {attributes(m)["tether.model.provider"] for m in models} == {"scripted"}
    [submitted] = named(finished, "POST /v1/tasks/runs")
    assert attributes(submitted)["http.response.status_code"] == 201
    assert attributes(submitted)["tether.run_id"] == str(run_id)
    assert attributes(submitted)["tether.workspace_id"] == WS


# --- metrics --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_for_a_governed_run_with_an_approval(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, metric_values: Delta
) -> None:
    await remediate(runtime, seeded, tmp_path)
    changed = metric_values()
    assert changed[key("tether.runs.started")] == 1
    assert changed[key("tether.runs.finished", status="succeeded")] == 1
    assert changed[key("tether.run.duration", status="succeeded")] == 1
    assert changed[key("tether.policy.decisions", decision="allow", tool="get_service_health")] == 1
    assert (
        changed[key("tether.policy.decisions", decision="require_approval", tool="restart_service")]
        == 1
    )
    assert changed[key("tether.approval.wait_duration", status="approved")] == 1
    assert changed[key("tether.tool.duration", tool="get_service_health", outcome="completed")] == 1
    assert changed[key("tether.tool.duration", tool="restart_service", outcome="completed")] == 1
    assert changed[key("tether.model.call.duration", provider="scripted", outcome="succeeded")] == 3
    assert total(changed, "tether.job.duration") >= 5
    assert total(changed, "tether.job.pickup_latency") == total(changed, "tether.job.duration")
    assert total(changed, "tether.tool.retries") == 0
    assert total(changed, "tether.ledger.indeterminate") == 0


@pytest.mark.asyncio
async def test_metrics_for_retries_an_indeterminate_write_and_its_reconciliation(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, metric_values: Delta
) -> None:
    run_id = await submit(seeded, tmp_path, "flaky")
    await drain(runtime)
    assert status(seeded, run_id) == "awaiting_reconciliation"
    [(entry_id,)] = rows(seeded, "SELECT id FROM tether.ledger_entries WHERE run_id = %s", run_id)
    reconciled = await call(
        seeded,
        tmp_path,
        "POST",
        f"/v1/ops/ledger/{entry_id}/reconcile",
        headers={"Authorization": f"Bearer {token('dave', ['reconciler', 'sre'], OPS)}"},
        json={"outcome": "not_applied", "justification": "checked the target"},
    )
    assert reconciled.status_code == 200, reconciled.text
    await drain(runtime)
    assert status(seeded, run_id) == "succeeded"
    changed = metric_values()
    # PD-14 send budget 3: two retries, then the unknown outcome is final.
    assert changed[key("tether.tool.retries", tool="probe_flaky")] == 2
    assert changed[key("tether.ledger.indeterminate", tool="probe_flaky")] == 1
    assert changed[key("tether.reconciliations", outcome="not_applied")] == 1
    assert changed[key("tether.tool.duration", tool="probe_flaky", outcome="unknown")] == 3


@pytest.mark.asyncio
async def test_metrics_for_a_dead_lettered_run(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, metric_values: Delta
) -> None:
    run_id = await submit(seeded, tmp_path, "read")
    rows(
        seeded,
        "UPDATE tether.jobs SET attempts = max_attempts"
        " WHERE run_id = %s AND kind = 'advance_run' RETURNING id",
        run_id,
    )
    await drain(runtime)
    assert status(seeded, run_id) == "failed"
    changed = metric_values()
    assert changed[key("tether.jobs.dead_lettered", kind="advance_run")] == 1
    assert changed[key("tether.runs.finished", status="failed")] == 1


@pytest.mark.asyncio
async def test_metrics_count_only_committed_transactions(seeded: Any) -> None:
    engine = create_database_engine(seeded.db.app_dsn)
    seen: list[str] = []

    def failing_callback() -> None:
        raise RuntimeError("a bug in a callback")

    async def roll_back() -> None:
        async with UnitOfWork(engine) as uow:
            uow.on_commit(lambda: seen.append("rolled back"))
            raise RuntimeError("abort")

    try:
        with pytest.raises(RuntimeError, match="abort"):
            await roll_back()
        async with UnitOfWork(engine) as uow:
            uow.on_commit(lambda: seen.append("committed"))
            uow.on_commit(failing_callback)
            uow.on_commit(lambda: seen.append("after a failing callback"))
    finally:
        await engine.dispose()
    assert seen == ["committed", "after a failing callback"]


@pytest.mark.asyncio
async def test_no_metric_label_carries_a_run_user_or_argument(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, metric_values: Delta
) -> None:
    run_id = await remediate(runtime, seeded, tmp_path)
    labels = {str(value) for (_, attrs) in metric_values() for _, value in attrs}
    assert labels
    for forbidden in (str(run_id), "alice", "bob", "checkout", WS, TASK_MESSAGE):
        assert not any(forbidden in label for label in labels), forbidden


# --- containment (T-16 core) ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_and_payloads_never_reach_logs_spans_or_metric_labels(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    spans: InMemorySpanExporter,
    raw_log: io.StringIO,
    metric_values: Delta,
) -> None:
    ISSUED.clear()
    await remediate(runtime, seeded, tmp_path)
    flaky = await submit(seeded, tmp_path, "flaky")
    await drain(runtime)
    assert status(seeded, flaky) == "awaiting_reconciliation"

    log_text = raw_log.getvalue()
    finished = spans.get_finished_spans()
    # Tether's api emits no FastAPI-native spans (only RequestSpans), so every span outside the
    # in-process demo target's own traces is Tether's, and all of those are scanned.
    demo = demo_target_traces(finished)
    ours = [s for s in finished if s.context is not None and s.context.trace_id not in demo]
    assert ours
    assert not [s.name for s in ours if _scope_name(s) != "tether"]
    span_text = "\n".join(span.to_json() for span in ours)
    label_text = "\n".join(str(v) for (_, attrs) in metric_values() for _, v in attrs)
    assert "job processed" in log_text  # the scan saw real runtime logs
    assert len(ISSUED) == 3
    forbidden = {
        "api key": APP_KEY.raw,
        "api key secret": APP_KEY.raw.rsplit("_", 1)[1],
        "audit key": os.environ["TETHER_AUDIT_KEY_TESTA1"],
        "binding key": os.environ["TETHER_BINDING_KEY_TESTB1"],
        "database password": seeded.db.app_password,
        "task input": TASK_MESSAGE,
        "tool argument": "checkout",
        **{f"jwt {i}": issued for i, issued in enumerate(ISSUED)},
    }
    for sink, text in (("logs", log_text), ("spans", span_text), ("metric labels", label_text)):
        for what, value in forbidden.items():
            assert value not in text, f"{what} found in {sink}"


# --- telemetry never breaks execution -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_collector_does_not_break_a_run(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    dead = BatchSpanProcessor(
        OTLPSpanExporter(endpoint="http://127.0.0.1:9/v1/traces", timeout=0.5),
        schedule_delay_millis=10,
    )
    provider.add_span_processor(dead)
    try:
        run_id = await remediate(runtime, seeded, tmp_path)
    finally:
        dead.shutdown()
    assert status(seeded, run_id) == "succeeded"
    view = await call(seeded, tmp_path, "GET", f"/v1/tasks/runs/{run_id}", headers=alice())
    assert view.json()["audit_chain_valid"] is True


@pytest.mark.asyncio
async def test_broken_telemetry_never_breaks_a_run(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Any:
        raise RuntimeError("telemetry is down")

    class Broken(dict[str, Any]):
        def __getitem__(self, name: str) -> Any:
            raise RuntimeError("telemetry is down")

    monkeypatch.setattr(tracing, "tracer", broken)
    monkeypatch.setattr(metrics, "_INSTRUMENTS", Broken())
    run_id = await remediate(runtime, seeded, tmp_path)
    assert status(seeded, run_id) == "succeeded"
    # Postgres stays authoritative: the run completed without any trace context at all.
    assert run_trace_parent(seeded, run_id) is None
