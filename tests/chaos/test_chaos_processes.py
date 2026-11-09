"""C-01 / C-03 with real processes: hard-kill and restart the api and the worker mid-run.

The api, the worker and the demo target run as separate OS processes against the test Postgres,
exactly as in deployment (``tether-api``, ``tether-worker``). Kills are hard (``SIGKILL`` on
Linux, ``TerminateProcess`` on Windows): no cleanup code runs. Waits poll database state.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.chaos.harness import TERMINAL, WS, Env, make_env

pytestmark = pytest.mark.chaos
REPO = Path(__file__).resolve().parents[2]

# psycopg's async driver needs the selector loop on Windows (production runs on Linux).
_PRELUDE = (
    "import asyncio, sys\n"
    "if sys.platform == 'win32':\n"
    "    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())\n"
)
# On Linux this is exactly the ``tether-api`` entrypoint. uvicorn picks the Proactor loop on
# Windows, so there the same startup runs with ``loop="none"`` (the selector policy above).
API = _PRELUDE + (
    "if sys.platform != 'win32':\n"
    "    from tether.api.app import run\n"
    "    run()\n"
    "else:\n"
    "    import uvicorn\n"
    "    from tether.api.app import create_app\n"
    "    from tether.observability.telemetry import setup_telemetry\n"
    "    from tether.settings import Settings\n"
    "    settings = Settings()\n"
    "    setup_telemetry(settings, service='tether-api')\n"
    "    uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port,\n"
    "                log_config=None, loop='none')\n"
)
WORKER = _PRELUDE + "from tether.worker.main import main\nmain()\n"
DEMO = (
    "import sys, uvicorn\n"
    "from services.demo_target_service.app import app\n"
    "uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), log_level='warning')\n"
)
Stack = tuple[Env, dict[str, "Process"], str, str]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Process:
    def __init__(self, name: str, code: str, env: dict[str, str], logs: Path, *args: str) -> None:
        self.name, self.code, self.env, self.logs, self.args = name, code, env, logs, args
        self.proc: subprocess.Popen[bytes] | None = None
        self.starts = 0

    def start(self) -> None:
        self.starts += 1
        log = (self.logs / f"{self.name}-{self.starts}.log").open("wb")
        self.proc = subprocess.Popen(  # noqa: S603 - our own interpreter running our own code
            [sys.executable, "-c", self.code, *self.args],
            cwd=REPO,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()  # hard: no shutdown handlers; its connections die with the process
            self.proc.wait(timeout=10)


def wait_for(predicate: Callable[[], bool], what: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


def http_ok(url: str) -> bool:
    try:
        return httpx.get(url, timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture
def stack(seeded: Any, tmp_path: Path) -> Iterator[Stack]:
    env = make_env(seeded.db, tmp_path)
    env.settings()  # writes the bootstrap file the processes load
    api_port, demo_port = free_port(), free_port()
    common = {
        **os.environ,
        "TETHER_ENV": "test",
        "TETHER_DATABASE_URL": seeded.db.app_dsn,
        "TETHER_BOOTSTRAP_FILE": str(tmp_path / "bootstrap.yaml"),
        "TETHER_SCENARIOS_DIR": str(tmp_path),
        "DEMO_TARGET_URL": f"http://127.0.0.1:{demo_port}",
        "TETHER_API_HOST": "127.0.0.1",
        "TETHER_API_PORT": str(api_port),
        # A short lease, so a killed worker's job is taken over quickly (PD-11 chaos override).
        "TETHER_WORKER_LEASE_SECONDS": "2",
        "TETHER_WORKER_POLL_INTERVAL_SECONDS": "0.1",
        "TETHER_WORKER_HEARTBEAT_FILE": str(tmp_path / "worker.heartbeat"),
        "PYTHONPATH": str(REPO),
    }
    common.pop("TETHER_OTEL_ENDPOINT", None)
    logs = tmp_path / "logs"
    logs.mkdir()
    procs = {
        "demo": Process("demo", DEMO, common, logs, str(demo_port)),
        "api": Process("api", API, common, logs),
        "worker": Process("worker", WORKER, common, logs),
    }
    for proc in procs.values():
        proc.start()
    api, demo = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{demo_port}"
    try:
        wait_for(lambda: http_ok(f"{demo}/healthz"), "the demo target")
        wait_for(lambda: http_ok(f"{api}/readyz"), "the api")
        yield env, procs, api, demo
    finally:
        for proc in procs.values():
            proc.kill()


def submit(api: str) -> uuid.UUID:
    response = httpx.post(
        f"{api}/v1/tasks/runs",
        json={
            "dev_identity": {
                "workspace_id": WS,
                "agent_id": "agent-1",
                "requester": {"sub": "alice", "roles": ["operator"]},
            },
            "task_input": {"message": "chaos #scenario=remediate"},
        },
        timeout=10,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["run_id"])


def approve(api: str, approval_id: Any) -> int:
    response = httpx.post(
        f"{api}/v1/approvals/{approval_id}/approve",
        json={
            "dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]},
            "reason": "chaos approval",
        },
        timeout=10,
    )
    return response.status_code


@pytest.mark.asyncio
async def test_c01_c03_hard_killing_the_api_and_worker_mid_run_loses_and_duplicates_nothing(
    stack: Stack,
) -> None:
    env, procs, api, demo = stack

    def statuses(runs: list[uuid.UUID]) -> set[str]:
        return {env.status(r) for r in runs}

    def leased_or_done(runs: list[uuid.UUID]) -> bool:
        leased = env.sql(
            "SELECT 1 FROM tether.jobs WHERE run_id = ANY(%s) AND state = 'leased'", runs
        )
        return bool(leased) or statuses(runs) <= TERMINAL

    runs: list[uuid.UUID] = []
    for cycle in range(3):
        batch = [submit(api) for _ in range(3)]
        runs += batch
        # C-01: the api dies with runs in flight; the worker carries on; the api comes back.
        procs["api"].kill()
        procs["api"].start()
        wait_for(lambda: http_ok(f"{api}/readyz"), "the restarted api")
        wait_for(lambda b=batch: statuses(b) == {"awaiting_approval"}, "approval waits")
        # C-03: everything dies while approvals are pending; the approvals stay actionable.
        procs["worker"].kill()
        procs["api"].kill()
        procs["api"].start()
        procs["worker"].start()
        wait_for(lambda: http_ok(f"{api}/readyz"), "the restarted api")
        for run_id in batch:
            assert approve(api, env.approval_id(run_id)) == 200
        # The worker dies with work leased (the restart being authorized or sent), then returns.
        wait_for(lambda b=batch: leased_or_done(b), "leased work")
        procs["worker"].kill()
        killed_holding = env.sql(
            "SELECT kind FROM tether.jobs WHERE run_id = ANY(%s) AND state = 'leased'", batch
        )
        held = [kind for (kind,) in killed_holding]
        sys.stdout.write(f"cycle {cycle}: worker killed holding leases on {held}\n")
        procs["worker"].start()
        wait_for(lambda b=batch: statuses(b) <= TERMINAL, f"cycle {cycle} to finish", 90)

    assert statuses(runs) == {"succeeded"}
    restarts = httpx.get(f"{demo}/services/checkout", timeout=5).json()["restarts"]
    assert restarts == len(runs)  # one applied restart per run, despite every kill
    for run_id in runs:
        assert [(t, s) for _, t, s, _, _ in env.ledger(run_id)] == [
            ("get_service_health", "completed"),
            ("restart_service", "completed"),
        ]
        await env.assert_reconstructable(run_id)
    assert env.sql("SELECT count(*) FROM tether.jobs WHERE state = 'dead'") == [(0,)]
