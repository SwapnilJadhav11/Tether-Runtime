"""T-31: governed runs of the change-control workload on the scripted provider, GitHub mocked.

The real ``config/bootstrap/change-control.yaml`` is loaded, so the worker's startup checks (risk,
timeouts, permissions) run against the shipped file. GitHub is an in-process fake that counts
every request, so "exactly one send" and "no second send" are observable facts.
"""

from __future__ import annotations

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
from tests.support.fake_github import REPO, FakeGitHub
from workloads import change_control
from workloads.change_control import client as github_client

from tether.api.app import create_app
from tether.cli.bootstrap import apply_bootstrap
from tether.core.statuses import JobKind
from tether.policy.config import load_bootstrap
from tether.providers.history import ModelTurn, NeutralMessage
from tether.queue import backoff
from tether.runtime.driver import WorkerRuntime
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_FILE = ROOT / "config" / "bootstrap" / "change-control.yaml"
SCENARIOS = Path(change_control.__file__).parent / "scenarios"
BOOT = load_bootstrap(BOOTSTRAP_FILE)
WS = "change-control"
AGENT = "change-controller"
APPROVER = ("bob", ["approver"])


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


def _settings(db: Any, scenarios: Path) -> Settings:
    return Settings(
        database_url=SecretStr(db.app_dsn),
        env="test",
        bootstrap_file=BOOTSTRAP_FILE,
        scenarios_dir=scenarios,
        worker_poll_interval_seconds=0.05,
    )


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    monkeypatch.setenv("CHANGE_CONTROL_GITHUB_REPO", REPO)
    monkeypatch.setenv("CHANGE_CONTROL_GITHUB_TOKEN", "ghp_integration_test_token")  # synthetic
    monkeypatch.delenv("CHANGE_CONTROL_GITHUB_API_URL", raising=False)
    # Retries and verification probes are immediate.
    monkeypatch.setattr(backoff, "tool_backoff_seconds", lambda attempt, rng=None: 0.0)
    monkeypatch.setattr(backoff, "verify_backoff_seconds", lambda declared, attempt, rng=None: 0.0)
    github_client.set_transport(fake.transport())
    yield fake
    github_client.set_transport(None)


@pytest.fixture
def scenarios(tmp_path: Path) -> Path:
    """The shipped scenarios plus one with a different proposal, for the changed-call test."""
    folder = tmp_path / "scenarios"
    folder.mkdir()
    for source in SCENARIOS.glob("*.yaml"):
        (folder / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    changed = yaml.safe_load((SCENARIOS / "open_change.yaml").read_text(encoding="utf-8"))
    changed["name"] = "open_change_edited"
    changed["turns"][1]["tool_calls"][0]["arguments"]["title"] = "Rotate the deploy key (edited)"
    (folder / "open_change_edited.yaml").write_text(yaml.safe_dump(changed), encoding="utf-8")
    return folder


@pytest_asyncio.fixture
async def runtime(seeded: Any, scenarios: Path, github: FakeGitHub) -> AsyncIterator[WorkerRuntime]:
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    provider = RecordingProvider(ScriptedProvider(scenarios, env="test"))
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, scenarios), bootstrap=BOOT, provider=provider
    )
    try:
        yield runtime
    finally:
        await runtime.close()


async def call_api(
    seeded: Any, scenarios: Path, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    app = create_app(_settings(seeded.db, scenarios))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http,
    ):
        return await http.request(method, url, **kwargs)


async def submit(seeded: Any, scenarios: Path, scenario: str) -> uuid.UUID:
    body = {
        "dev_identity": {
            "workspace_id": WS,
            "agent_id": AGENT,
            "requester": {"sub": "alice", "roles": ["operator"]},
        },
        "task_input": {"message": f"manage changes #scenario={scenario}"},
    }
    response = await call_api(seeded, scenarios, "POST", "/v1/tasks/runs", json=body)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


async def decide(
    seeded: Any, scenarios: Path, approval_id: Any, action: str, who: tuple[str, list[str]]
) -> httpx.Response:
    body = {
        "dev_identity": {"workspace_id": WS, "sub": who[0], "roles": who[1]},
        "reason": "reviewed",
    }
    return await call_api(
        seeded, scenarios, "POST", f"/v1/approvals/{approval_id}/{action}", json=body
    )


async def reconcile(seeded: Any, scenarios: Path, ledger_id: Any, outcome: str) -> httpx.Response:
    body = {
        "dev_identity": {"workspace_id": WS, "sub": "dave", "roles": ["reconciler"]},
        "outcome": outcome,
        "justification": "checked the repository by hand",
    }
    return await call_api(
        seeded, scenarios, "POST", f"/v1/ops/ledger/{ledger_id}/reconcile", json=body
    )


async def drain(runtime: WorkerRuntime, kinds: Sequence[str] | None = None) -> None:
    for _ in range(60):
        if not await process_next_job(runtime, kinds=kinds):
            return
    raise AssertionError("the queue did not drain")


def rows(seeded: Any, statement: str, *params: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        return conn.execute(statement, params).fetchall()


def run_status(seeded: Any, run_id: uuid.UUID) -> tuple[Any, ...]:
    return rows(seeded, "SELECT status, outcome_summary FROM tether.runs WHERE id = %s", run_id)[0]


def approvals(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return rows(
        seeded,
        "SELECT id, status, tool, call_hash, arguments_masked FROM tether.approvals"
        " WHERE run_id = %s ORDER BY created_at",
        run_id,
    )


def ledger(seeded: Any, run_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return rows(
        seeded,
        "SELECT id, tool, state, attempts, resolution FROM tether.ledger_entries"
        " WHERE run_id = %s ORDER BY step_ordinal",
        run_id,
    )


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event_type for (event_type,) in found]


async def chain_valid(seeded: Any, scenarios: Path, run_id: uuid.UUID) -> bool:
    response = await call_api(
        seeded, scenarios, "GET", f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
    )
    assert response.status_code == 200, response.text
    return bool(response.json()["audit_chain_valid"])


# Every verification probe (three by default) finds the issue listing unavailable: automatic
# verification cannot establish the outcome and hands it to a person (ADR-0019 rule 11).
UNREADABLE = [("GET", "issues", "503")] * 3


def assert_verification_exhausted(seeded: Any, run_id: uuid.UUID) -> None:
    trail = events(seeded, run_id)
    assert trail.count("VERIFICATION_INCONCLUSIVE") == 3
    assert trail.count("VERIFICATION_EXHAUSTED") == 1
    assert "VERIFIED" not in trail


async def awaiting_create_approval(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, scenario: str = "open_change"
) -> uuid.UUID:
    run_id = await submit(seeded, scenarios, scenario)
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "awaiting_approval"
    return run_id


# --- T-31: both scenarios end to end ----------------------------------------------------------


@pytest.mark.asyncio
async def test_t31_the_shipped_bootstrap_passes_the_worker_startup_checks(
    runtime: WorkerRuntime,
) -> None:
    assert change_control.WORKLOAD.name in runtime.registries


@pytest.mark.asyncio
async def test_t31_open_change_pauses_for_approval_then_creates_the_issue_once(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    assert github.writes() == []  # nothing sent before approval
    [(approval_id, status, tool, _, arguments)] = approvals(seeded, run_id)
    assert (status, tool) == ("pending", "create_change_issue")
    response = await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    assert response.status_code == 200, response.text
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "opened the change issue after approval.")
    [sent] = github.writes()
    assert (sent.method, sent.path) == ("POST", f"/repos/{REPO}/issues")
    assert sent.body["title"] == arguments["title"]
    assert sent.body["body"].startswith(arguments["body"])
    assert [(e[1], e[2]) for e in ledger(seeded, run_id)] == [
        ("list_open_change_issues", "completed"),
        ("create_change_issue", "completed"),
    ]
    assert await chain_valid(seeded, scenarios, run_id)
    trail = events(seeded, run_id)
    assert "APPROVAL_DECIDED" in trail
    assert trail[-1] == "RUN_FINISHED"


@pytest.mark.asyncio
async def test_t31_label_and_close_applies_the_label_without_approval_and_pauses_for_close(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    github.add_issue("Rotate the deploy key", labels=("change-control",))
    run_id = await submit(seeded, scenarios, "label_and_close")
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "awaiting_approval"
    [(approval_id, status, tool, *_)] = approvals(seeded, run_id)
    assert (status, tool) == ("pending", "close_issue")  # the label needed no approval
    assert [s.path.rsplit("/", 1)[-1] for s in github.writes()] == ["labels"]
    assert github.issues[1]["state"] == "open"
    await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    await drain(runtime)
    assert run_status(seeded, run_id) == ("succeeded", "labelled the issue approved and closed it.")
    assert github.issues[1]["state"] == "closed"
    assert {label["name"] for label in github.issues[1]["labels"]} == {"change-control", "approved"}
    assert await chain_valid(seeded, scenarios, run_id)


@pytest.mark.asyncio
async def test_t31_the_approval_is_bound_to_the_exact_title_and_body(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    first = await awaiting_create_approval(runtime, seeded, scenarios, "open_change")
    second = await awaiting_create_approval(runtime, seeded, scenarios, "open_change_edited")
    [(_, _, _, hash_one, args_one)] = approvals(seeded, first)
    [(_, _, _, hash_two, args_two)] = approvals(seeded, second)
    assert args_one["title"] == "Rotate the deploy key"
    assert args_two["title"] == "Rotate the deploy key (edited)"
    assert hash_one != hash_two  # a changed proposal is a new approval
    assert github.writes() == []


@pytest.mark.asyncio
async def test_t31_a_rejected_create_is_never_sent(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    [(approval_id, *_)] = approvals(seeded, run_id)
    response = await decide(seeded, scenarios, approval_id, "reject", APPROVER)
    assert response.status_code == 200, response.text
    await drain(runtime)
    assert github.writes() == []
    assert [e[1:3] for e in ledger(seeded, run_id) if e[1] == "create_change_issue"] == []
    assert run_status(seeded, run_id)[0] == "succeeded"
    assert await chain_valid(seeded, scenarios, run_id)


# --- ambiguity: timeouts, crashes, retries ----------------------------------------------------


@pytest.mark.asyncio
async def test_t31_a_create_that_times_out_is_indeterminate_after_exactly_one_request(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    [(approval_id, *_)] = approvals(seeded, run_id)
    github.script[:] = [
        ("POST", "issues", "timeout_after"),  # applied, but no response
        *UNREADABLE,  # and the read-back cannot establish it: a person must (ADR-0019 rule 11)
    ]
    await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    await drain(runtime)
    assert len(github.writes()) == 1
    create = [e for e in ledger(seeded, run_id) if e[1] == "create_change_issue"]
    assert [(e[2], e[3]) for e in create] == [("indeterminate", 1)]
    assert_verification_exhausted(seeded, run_id)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"
    await drain(runtime)
    assert len(github.writes()) == 1  # never re-sent, however long the run waits
    # The marker makes the human's question answerable: the issue is there exactly once.
    assert len(github.issues) == 1

    response = await reconcile(seeded, scenarios, create[0][0], "applied")
    assert response.status_code == 200, response.text
    await drain(runtime)
    assert run_status(seeded, run_id)[0] == "succeeded"
    assert len(github.writes()) == 1
    assert await chain_valid(seeded, scenarios, run_id)
    assert "RECONCILED" in events(seeded, run_id)


@pytest.mark.asyncio
async def test_t31_a_crash_after_the_create_was_dispatched_sends_no_second_request(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    [(approval_id, *_)] = approvals(seeded, run_id)
    await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    await process_next_job(runtime, kinds=(JobKind.ADVANCE_RUN,))
    # A crash after TX6: the entry is dispatched with no outcome and the job comes back.
    changed = rows(
        seeded,
        "UPDATE tether.ledger_entries SET state = 'dispatched', attempts = 1"
        " WHERE run_id = %s AND tool = 'create_change_issue' AND state = 'pending' RETURNING id",
        run_id,
    )
    assert len(changed) == 1
    github.script[:] = list(UNREADABLE)  # the read-back cannot establish what happened
    await drain(runtime)
    assert github.writes() == []
    create = [e for e in ledger(seeded, run_id) if e[1] == "create_change_issue"]
    assert [e[2] for e in create] == ["indeterminate"]
    assert_verification_exhausted(seeded, run_id)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


@pytest.mark.asyncio
async def test_t31_a_label_that_times_out_once_is_re_sent_and_applied_once(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    github.add_issue("Rotate the deploy key", labels=("change-control",))
    github.script[:] = [("POST", "labels", "timeout_after")]  # applied, then no response
    run_id = await submit(seeded, scenarios, "label_and_close")
    await drain(runtime)
    label_sends = [s for s in github.writes() if s.path.endswith("/labels")]
    assert len(label_sends) == 2  # the same idempotent write, re-sent once
    assert [label["name"] for label in github.issues[1]["labels"]].count("approved") == 1
    [label_entry] = [e for e in ledger(seeded, run_id) if e[1] == "apply_label"]
    assert (label_entry[2], label_entry[3]) == ("completed", 2)
    assert run_status(seeded, run_id)[0] == "awaiting_approval"  # on to the close


# --- honest outcome classes through the ledger ------------------------------------------------


@pytest.mark.asyncio
async def test_t31_a_github_rejection_of_the_create_is_a_definite_not_applied(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    [(approval_id, *_)] = approvals(seeded, run_id)
    github.script[:] = [("POST", "issues", "422")]
    await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    await drain(runtime)
    create = [e for e in ledger(seeded, run_id) if e[1] == "create_change_issue"]
    assert [e[2] for e in create] == ["failed"]
    outcome = rows(
        seeded,
        "SELECT outcome_class FROM tether.ledger_entries"
        " WHERE run_id = %s AND tool = 'create_change_issue'",
        run_id,
    )
    assert outcome == [("not_applied",)]
    assert len(github.writes()) == 1
    assert run_status(seeded, run_id)[0] == "succeeded"


@pytest.mark.asyncio
async def test_t31_a_server_error_on_the_create_is_unknown_not_a_failure(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    [(approval_id, *_)] = approvals(seeded, run_id)
    github.script[:] = [("POST", "issues", "503"), *UNREADABLE]
    await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    await drain(runtime)
    create = [e for e in ledger(seeded, run_id) if e[1] == "create_change_issue"]
    assert [e[2] for e in create] == ["indeterminate"]
    assert len(github.writes()) == 1
    assert_verification_exhausted(seeded, run_id)
    assert run_status(seeded, run_id)[0] == "awaiting_reconciliation"


# --- W5.1: automatic verification by the marker read-back (ADR-0019, T-27) ----------------------


@pytest.mark.asyncio
async def test_t27_a_create_that_timed_out_after_applying_is_verified_applied_without_a_person(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    [(approval_id, *_)] = approvals(seeded, run_id)
    github.script[:] = [("POST", "issues", "timeout_after")]  # applied, but no response
    await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    await drain(runtime)
    [create] = [e for e in ledger(seeded, run_id) if e[1] == "create_change_issue"]
    assert (create[2], create[4]) == ("completed", "verified_applied")
    assert len(github.writes()) == 1  # verified by reads only, never re-sent
    assert len(github.issues) == 1
    assert run_status(seeded, run_id)[0] == "succeeded"
    trail = events(seeded, run_id)
    assert "VERIFIED" in trail
    assert "RECONCILED" not in trail
    assert await chain_valid(seeded, scenarios, run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["503", "timeout_before"])
async def test_t27_a_create_that_never_arrived_is_verified_not_applied(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub, failure: str
) -> None:
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    [(approval_id, *_)] = approvals(seeded, run_id)
    github.script[:] = [("POST", "issues", failure)]
    await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    await drain(runtime)
    [create] = [e for e in ledger(seeded, run_id) if e[1] == "create_change_issue"]
    assert (create[2], create[4]) == ("failed", "verified_not_applied")
    assert len(github.writes()) == 1
    assert github.issues == {}
    assert run_status(seeded, run_id)[0] == "succeeded"


@pytest.mark.asyncio
async def test_t27_change_control_verification_only_reads(
    runtime: WorkerRuntime, seeded: Any, scenarios: Path, github: FakeGitHub
) -> None:
    """The probe is a GET of the issue listing with the change-control label; nothing else."""
    run_id = await awaiting_create_approval(runtime, seeded, scenarios)
    [(approval_id, *_)] = approvals(seeded, run_id)
    github.script[:] = [("POST", "issues", "timeout_after")]
    before = len(github.seen)
    await decide(seeded, scenarios, approval_id, "approve", APPROVER)
    await drain(runtime)
    after = github.seen[before:]
    assert [s.method for s in after] == ["POST", "GET"]
    probe = after[1]
    assert probe.path == f"/repos/{REPO}/issues"
    assert probe.query["labels"] == "change-control"
    assert run_status(seeded, run_id)[0] == "succeeded"
