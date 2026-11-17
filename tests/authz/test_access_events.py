"""W4.3 / G2: access events for run inspections through the approvals and ops audiences.

PRD: run inspections produce access events: operational logs, persisted asynchronously, separate
from the authoritative HMAC chain, and never able to turn a permitted read into an error. The
writer buffers (bounded), flushes every second or when a batch fills, and counts what it cannot
keep.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from tests.authz.fixtures import (
    APPROVALS,
    FIXTURE_B,
    OPS,
    OPS_DEMO,
    TASKS,
    World,
    headers,
    person,
    requester,
    token,
)

from tether.audit import access_events
from tether.audit.access_events import AccessEvent, AccessEventWriter, writer_for
from tether.storage.engine import create_database_engine

AUDITOR = person(OPS_DEMO, OPS, "audrey", ["auditor"])
ADMIN = person(OPS_DEMO, OPS, "adam", ["workspace_admin"])
BOB = person(OPS_DEMO, APPROVALS, "bob", ["approver", "oncall"])
Metrics = Callable[[], dict[Any, float]]
DROPPED = ("tether.access_events.dropped", frozenset())


def stored(world: World) -> list[tuple[Any, ...]]:
    return world.rows(
        "SELECT workspace_id, actor, audience, action, target_run_id, details"
        " FROM tether.access_events ORDER BY created_at, actor"
    )


@pytest.mark.asyncio
async def test_t24_g2_an_ops_inspection_is_recorded_after_a_flush(world: World) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    async with world.serving() as (app, http):
        response = await http.get(f"/v1/ops/runs/{run_id}", headers=AUDITOR)
        assert response.status_code == 200, response.text
        await writer_for(app.state).flush()
    [event] = stored(world)
    assert event[:5] == (OPS_DEMO, "audrey", OPS, "run.inspect", run_id)
    assert event[5] == {"roles": ["auditor"]}


@pytest.mark.asyncio
async def test_t24_g2_a_clean_shutdown_flushes_what_is_still_buffered(world: World) -> None:
    """The api lifespan closes the writer before the engine goes: no flush call, no timer."""
    run_id = await world.submit(OPS_DEMO, "restart")
    async with world.serving() as (app, http):
        # A writer whose timer never fires inside this test: only the shutdown can write.
        app.state.access_events = AccessEventWriter(app.state.db_engine, flush_interval=3600)
        response = await http.get(f"/v1/ops/runs/{run_id}", headers=AUDITOR)
        assert response.status_code == 200, response.text
        assert stored(world) == [], "still buffered while the app is serving"
    [event] = stored(world)
    assert event[:5] == (OPS_DEMO, "audrey", OPS, "run.inspect", run_id)
    assert app.state.access_events.record(_event(run_id)) is False, "closed at shutdown"


@pytest.mark.asyncio
async def test_t24_g2_an_approver_inspection_through_the_approvals_audience_is_recorded(
    world: World,
) -> None:
    run_id = await world.submit(OPS_DEMO, "restart", sub="alice")
    async with world.serving() as (app, http):
        response = await http.get(f"/v1/approvals/runs/{run_id}", headers=BOB)
        assert response.status_code == 200, response.text
        await writer_for(app.state).flush()
    [event] = stored(world)
    assert event[:5] == (OPS_DEMO, "bob", APPROVALS, "run.inspect", run_id)


@pytest.mark.asyncio
async def test_t24_g2_every_inspection_is_one_event_for_its_own_workspace(world: World) -> None:
    mine = await world.submit(OPS_DEMO, "restart")
    theirs = await world.submit(FIXTURE_B, "restart", sub="mallory")
    other_admin = person(FIXTURE_B, OPS, "fred", ["workspace_admin"])
    async with world.serving() as (app, http):
        for _ in range(2):
            assert (await http.get(f"/v1/ops/runs/{mine}", headers=ADMIN)).status_code == 200
        assert (await http.get(f"/v1/ops/runs/{theirs}", headers=other_admin)).status_code == 200
        await writer_for(app.state).flush()
    found = stored(world)
    assert [(e[0], e[1], e[4]) for e in found] == [
        (OPS_DEMO, "adam", mine),
        (OPS_DEMO, "adam", mine),
        (FIXTURE_B, "fred", theirs),
    ]


@pytest.mark.asyncio
async def test_t24_g2_only_permitted_inspections_through_those_audiences_are_recorded(
    world: World,
) -> None:
    run_id = await world.submit(OPS_DEMO, "restart", sub="alice")
    foreign = person(FIXTURE_B, OPS, "frida", ["auditor"])
    nobody = person(OPS_DEMO, OPS, "nobody", [])
    ineligible = person(OPS_DEMO, APPROVALS, "ann", ["approver"])  # lacks the ``oncall`` role
    async with world.serving() as (app, http):
        attempts = [
            (f"/v1/ops/runs/{run_id}", foreign, 404),
            (f"/v1/ops/runs/{run_id}", nobody, 404),
            (f"/v1/ops/runs/{uuid.uuid4()}", AUDITOR, 404),
            (f"/v1/ops/runs/{run_id}", headers(), 401),
            (f"/v1/ops/runs/{run_id}", headers(token(OPS_DEMO, TASKS, "audrey", ["auditor"])), 401),
            (f"/v1/approvals/runs/{run_id}", ineligible, 404),
            (f"/v1/approvals/runs/{run_id}", headers(), 401),
            # The requester reading their own run through the tasks audience is not an access.
            (f"/v1/tasks/runs/{run_id}", requester(OPS_DEMO, "alice"), 200),
            # Listing and viewing approvals are not run inspections.
            ("/v1/approvals/approvals", BOB, 200),
        ]
        for url, caller, expected in attempts:
            response = await http.get(url, headers=caller)
            assert response.status_code == expected, (url, response.text)
        await writer_for(app.state).flush()
    assert stored(world) == []


@pytest.mark.asyncio
async def test_t24_g2_access_events_stay_out_of_the_audit_chain(world: World) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    before = world.rows("SELECT count(*) FROM tether.audit_events")
    async with world.serving() as (app, http):
        first = await http.get(f"/v1/ops/runs/{run_id}", headers=AUDITOR)
        await writer_for(app.state).flush()
        second = await http.get(f"/v1/ops/runs/{run_id}", headers=AUDITOR)
        await writer_for(app.state).flush()
    assert world.rows("SELECT count(*) FROM tether.audit_events") == before
    assert first.json()["audit_chain_valid"] is True
    assert second.json()["audit_chain_valid"] is True
    assert len(stored(world)) == 2


@pytest.mark.asyncio
async def test_t24_g2_a_failing_access_log_never_breaks_a_permitted_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")

    def broken(state: Any) -> AccessEventWriter:
        raise RuntimeError("the access log is down")

    monkeypatch.setattr(access_events, "writer_for", broken)
    for url, caller in (
        (f"/v1/ops/runs/{run_id}", AUDITOR),
        (f"/v1/approvals/runs/{run_id}", BOB),
    ):
        response = await world.request("GET", url, headers=caller)
        assert response.status_code == 200, (url, response.text)
    assert stored(world) == []


@pytest.mark.asyncio
async def test_t24_g2_events_are_buffered_until_a_flush(world: World) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    engine = create_database_engine(world.db.app_dsn)
    try:
        writer = AccessEventWriter(engine, flush_interval=3600, batch_size=100)
        assert writer.record(_event(run_id))
        await asyncio.sleep(0.2)
        assert stored(world) == [], "an event is written outside the request, not at record time"
        await writer.flush()
        assert len(stored(world)) == 1
        await writer.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_t24_g2_the_writer_flushes_on_its_own_interval_and_when_a_batch_fills(
    world: World,
) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    engine = create_database_engine(world.db.app_dsn)
    try:
        timed = AccessEventWriter(engine, flush_interval=0.05, batch_size=100)
        timed.record(_event(run_id, actor="timed"))
        await _eventually(lambda: len(stored(world)) == 1)
        await timed.aclose()
        batched = AccessEventWriter(engine, flush_interval=3600, batch_size=2)
        batched.record(_event(run_id, actor="batch-1"))
        batched.record(_event(run_id, actor="batch-2"))
        await _eventually(lambda: len(stored(world)) == 3)
        await batched.aclose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_t24_g2_a_full_buffer_drops_and_counts_instead_of_blocking(
    world: World, metric_values: Metrics
) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    engine = create_database_engine(world.db.app_dsn)
    try:
        writer = AccessEventWriter(engine, flush_interval=3600, batch_size=100, capacity=2)
        kept = [writer.record(_event(run_id, actor=f"a{i}")) for i in range(4)]
        assert kept == [True, True, False, False]
        assert metric_values()[DROPPED] == 2
        await writer.aclose()
        assert len(stored(world)) == 2
        assert writer.record(_event(run_id)) is False, "a closed writer keeps nothing"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_t24_g2_a_failed_write_is_counted_and_never_raised(
    world: World, metric_values: Metrics, log_lines: Callable[[], list[dict[str, Any]]]
) -> None:
    run_id = await world.submit(OPS_DEMO, "restart")
    engine = create_database_engine(world.db.app_dsn)
    try:
        writer = AccessEventWriter(engine, flush_interval=3600)
        writer.record(AccessEvent("no-such-workspace", "x", OPS, "run.inspect", run_id))
        writer.record(_event(run_id, actor="fine"))
        await writer.flush()  # must not raise
        assert metric_values()[DROPPED] == 1
        assert [e[1] for e in stored(world)] == ["fine"]
        assert any(line.get("event") == "access events not recorded" for line in log_lines())
        await writer.aclose()
    finally:
        await engine.dispose()


def _event(run_id: uuid.UUID, actor: str = "audrey") -> AccessEvent:
    return AccessEvent(OPS_DEMO, actor, OPS, "run.inspect", run_id, {"roles": ["auditor"]})


async def _eventually(check: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the condition did not become true in time")
