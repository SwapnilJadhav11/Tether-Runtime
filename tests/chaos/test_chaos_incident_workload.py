"""C-05, C-06 and C-07 on the incident workload's real write tool (W9.3).

The harness's own chaos tests prove these on test-only probe tools. Here the write is the real
``restart_service`` going over HTTP to the real demo target, which keeps one idempotency record
per key and counts restarts: the target's restart counter is the side-effect count. A wrapper
around the in-process transport is where the process "dies" or the request is held in flight; it
never changes what the tool or the target do.

C-03 (kill while an approval is pending) and C-04 (kill after approval, before dispatch) are
already proven on the real tool: ``test_chaos_processes.py`` (real processes, approvals pending)
and ``test_chaos_crashes.py::test_c01_c04_*`` (a crash after ``EXECUTION_AUTHORIZED`` and after
``DISPATCHED`` of the real restart). F7b (lease expiry on a *non-idempotent* write) has no real
tool to run on: the workload has no non-idempotent write, so it stays on the probe tool in
``test_chaos_races.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from tests.chaos.harness import Crash, Env, restarts, wait_until
from tests.chaos.test_chaos_crashes import drive_through_crashes
from workloads.incident_remediation import client as demo_client

pytestmark = pytest.mark.chaos


class WireTap(httpx.AsyncBaseTransport):
    """Sits in front of the demo target. For each restart request, ``script`` says what
    happens: ``ok``; ``crash_before`` (the worker dies with the request held, before the target
    saw it); ``crash_after`` (the target applied it, then the worker dies before TX7).
    ``gate`` holds every restart request in flight until it is set."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.script: list[str] = []
        self.gate: asyncio.Event | None = None
        self.keys: list[str] = []
        self.in_flight = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/restart"):
            return await self.inner.handle_async_request(request)
        self.keys.append(request.headers["Idempotency-Key"])
        self.in_flight += 1
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.in_flight -= 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "crash_before":
            raise Crash("killed with the restart in flight, before the target applied it")
        response = await self.inner.handle_async_request(request)
        if step == "crash_after":
            raise Crash("killed after the target applied the restart, before TX7")
        return response


@pytest.fixture
def tap(demo_target: FastAPI) -> Iterator[WireTap]:
    tap = WireTap(httpx.ASGITransport(app=demo_target))
    demo_client.set_transport(tap)
    yield tap
    demo_client.set_transport(None)


@pytest.mark.parametrize("step", ["crash_before", "crash_after"])
@pytest.mark.parametrize("repeat", range(3))
@pytest.mark.asyncio
async def test_c05_c06_a_crash_with_the_real_restart_in_flight_restarts_the_service_once(
    env: Env, tap: WireTap, step: str, repeat: int
) -> None:
    tap.script[:] = [step]
    run_id, crashes, worker = await drive_through_crashes(env, None)  # health, then restart
    try:
        assert crashes == 1
        assert env.status(run_id) == "succeeded"
        # Redelivered with the same key: the target restarted the service exactly once.
        assert len(tap.keys) == 2
        assert len(set(tap.keys)) == 1
        assert await restarts() == 1
        assert [(o, t, s) for o, t, s, _, _ in env.ledger(run_id)] == [
            (0, "get_service_health", "completed"),
            (1, "restart_service", "completed"),
        ]
        assert env.ledger(run_id)[1][3] == 2  # two sends, one effect
        assert env.events(run_id).count("REDISPATCHED") == 1
        assert env.model_calls(run_id) == 3
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.parametrize("repeat", range(5))
@pytest.mark.asyncio
async def test_c07_overlapping_same_key_restarts_on_the_real_target_apply_once(
    env: Env, tap: WireTap, repeat: int
) -> None:
    first, second = await env.worker(), await env.worker()
    try:
        run_id = await env.submit("remediate")
        await env.drain(first)  # the read, then the run pauses for approval
        assert env.status(run_id) == "awaiting_approval"
        assert (await env.approve(env.approval_id(run_id))).status_code == 200
        gate = tap.gate = asyncio.Event()
        slow = asyncio.create_task(env.drain(first))
        await wait_until(lambda: tap.in_flight == 1)
        env.expire_leases()  # the slow request outlives its lease; another worker takes the job
        racing = asyncio.create_task(env.drain(second))
        await wait_until(lambda: tap.in_flight == 2)
        gate.set()
        await asyncio.gather(slow, racing)
        await env.drain(first, second)
        assert env.status(run_id) == "succeeded"
        assert len(tap.keys) == 2
        assert len(set(tap.keys)) == 1  # the same key on both requests
        assert await restarts() == 1  # the target applied it once
        assert env.events(run_id).count("TOOL_OUTCOME") == 2  # the read and one write outcome
        assert env.ledger(run_id)[1][2:4] == ("completed", 2)
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, first)
    finally:
        await first.close()
        await second.close()
