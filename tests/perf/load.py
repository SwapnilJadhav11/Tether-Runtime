"""Load driver for the perf suite (plan W10.3, tooling half; tests P-01..P-03).

Several in-process ``WorkerRuntime``s drain the queue concurrently on one event loop; runs are
submitted and decided through the real API app (one app for the whole test), on the scripted
provider and counting write tools. Workers can be "killed" at random points (a ``Crash`` raised by
the model call or a tool send) and replaced by fresh runtimes whose dead predecessor's lease is
expired, exactly as the chaos harness does.

Everything is measured, nothing is assumed: ``collect`` returns a ``Report`` and ``violations``
turns it into a list of correctness failures (empty means correct). The negative self-tests feed
``violations`` doctored reports, so the check is proven able to fail.

Numbers are for one event loop in one process against one Postgres on the same machine. They say
nothing about a multi-process deployment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.chaos.harness import (
    AGENT,
    TERMINAL,
    WS,
    Crash,
    Env,
    ProbeIn,
    ProbeOut,
    is_crash,
    restarts,
)
from workloads.incident_remediation import WORKLOAD

from tether.api.app import create_app
from tether.providers.history import ModelTurn, NeutralMessage
from tether.queue import postgres as queue
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, ToolContext, ToolDescriptor, Workload
from tether.testing.scripted import ScriptedProvider
from tether.worker.main import process_next_job

SCENARIO_READ = (
    "name: read\nturns:\n"
    "  - tool_calls: [{tool: get_service_health, arguments: {service: checkout}}]\n"
    "  - final: checked\n"
)


# --- parameters ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Params:
    runs: int = 100
    workers: int = 4
    unknown_fraction: float = 0.1
    crash_probability: float = 0.05
    seed: int = 1
    timeout_seconds: float = 600.0
    results_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "tether-perf")
    sample: int = 10  # runs checked with assert_reconstructable / assert_terminal_stable

    @classmethod
    def from_env(cls) -> Params:
        def number(name: str, default: Any, cast: Any) -> Any:
            return cast(os.environ.get(f"TETHER_PERF_{name}", default))

        results = os.environ.get("TETHER_PERF_RESULTS_DIR")
        defaults = cls()
        return cls(
            runs=number("RUNS", defaults.runs, int),
            workers=number("WORKERS", defaults.workers, int),
            unknown_fraction=number("UNKNOWN_FRACTION", defaults.unknown_fraction, float),
            crash_probability=number("CRASH_PROBABILITY", defaults.crash_probability, float),
            seed=number("SEED", defaults.seed, int),
            timeout_seconds=number("TIMEOUT_SECONDS", defaults.timeout_seconds, float),
            results_dir=Path(results) if results else defaults.results_dir,
            sample=number("SAMPLE", defaults.sample, int),
        )


class Coin:
    """A seeded, repeatable source of yes/no decisions (no global random state)."""

    def __init__(self, seed: int) -> None:
        self.seed, self.n = seed, 0

    def flip(self, probability: float) -> bool:
        self.n += 1
        digest = hashlib.sha256(f"{self.seed}:{self.n}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64 < probability


# --- the counting tools and the crashing provider -----------------------------------------------


@dataclass
class Fleet:
    """What the target saw. ``effects`` is applied-once-per-idempotency-key (a target that honours
    keys); ``nonidem_effects`` counts every application of the non-idempotent write."""

    coin: Coin
    crash_probability: float = 0.0
    drop_nonidem: bool = False  # every non-idempotent write applies, then loses its answer
    sends: int = 0
    keys: list[str] = field(default_factory=list)
    effects: set[str] = field(default_factory=set)
    nonidem_effects: int = 0
    crashes: Counter[str] = field(default_factory=Counter)

    def crash_here(self, where: str) -> bool:
        if self.crash_probability and self.coin.flip(self.crash_probability):
            self.crashes[where] += 1
            return True
        return False

    async def idem(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        self.sends += 1
        self.keys.append(ctx.idempotency_key)
        if self.crash_here("send_before"):
            raise Crash("killed with the write in flight, before the target applied it")
        self.effects.add(ctx.idempotency_key)
        if self.crash_here("send_after"):
            raise Crash("killed after the target applied the write, before TX7")
        return ProbeOut(target=args.target)

    async def nonidem(self, args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        self.sends += 1
        self.keys.append(ctx.idempotency_key)
        self.nonidem_effects += 1
        if self.drop_nonidem:
            raise httpx.RemoteProtocolError("connection dropped after the request was applied")
        return ProbeOut(target=args.target)


class LoadProvider(ScriptedProvider):
    def __init__(self, scenarios: Path, fleet: Fleet) -> None:
        super().__init__(scenarios, env="test")
        self.fleet = fleet

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        if self.fleet.crash_here("model_call"):
            raise Crash("killed during a model call")
        return await super().complete(history, manifest)


def load_workload(fleet: Fleet) -> Workload:
    async def idem(args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        return await fleet.idem(args, ctx)

    async def nonidem(args: ProbeIn, ctx: ToolContext) -> ProbeOut:
        return await fleet.nonidem(args, ctx)

    def probe(name: str, idempotency: Any, handler: Any) -> ToolDescriptor:
        return ToolDescriptor(
            name=name,
            version="1.0.0",
            description="Load test write tool.",
            input_model=ProbeIn,
            output_model=ProbeOut,
            effect="write",
            idempotency=idempotency,
            handler=handler,
            risk="low",
            required_permissions=("probe:write",),
        )

    probes = (
        probe("probe_write", "supported", idem),
        probe("probe_nonidem", "unsupported", nonidem),
    )
    return Workload(
        name=WORKLOAD.name,
        tools=(*WORKLOAD.tools, *probes),
        agent=AgentDefinition(
            system_prompt="load", tools=(*WORKLOAD.agent.tools, *(p.name for p in probes))
        ),
    )


# --- statistics and the report ------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no data."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


@dataclass
class Report:
    variant: str
    params: dict[str, Any]
    submitted: int
    expected: dict[str, int]  # status -> count
    statuses: dict[str, int]
    wall_seconds: float
    latency_seconds: dict[str, float | None]
    pickup_seconds: dict[str, float | None]
    job_attempts: dict[str, int]  # "kind:attempts" -> jobs
    leased_jobs: int
    dead_jobs: int
    stuck_runs: int
    effects_expected: dict[str, int]
    effects_actual: dict[str, int]
    chains_invalid: int
    crashes: dict[str, int]
    machine: dict[str, Any]

    @property
    def runs_per_second(self) -> float:
        return self.submitted / self.wall_seconds if self.wall_seconds else 0.0


def violations(report: Report) -> list[str]:
    """Every way the report is incorrect. Empty means every correctness property held."""
    found = []
    if report.statuses != report.expected:
        found.append(f"statuses {report.statuses} != expected {report.expected}")
    if report.stuck_runs:
        found.append(f"{report.stuck_runs} stuck run(s)")
    if report.leased_jobs:
        found.append(f"{report.leased_jobs} job(s) left leased")
    if report.dead_jobs:
        found.append(f"{report.dead_jobs} dead job(s)")
    if report.chains_invalid:
        found.append(f"{report.chains_invalid} invalid audit chain(s)")
    for name, wanted in report.effects_expected.items():
        actual = report.effects_actual.get(name)
        if actual != wanted:
            found.append(f"effects of {name}: {actual} != expected {wanted}")
    return found


def render(report: Report) -> str:
    def fmt(value: float | None) -> str:
        return "-" if value is None else f"{value * 1000:.0f} ms"

    lines = [
        f"== perf: {report.variant} ==",
        f"  runs {report.submitted}, workers {report.params['workers']}, "
        f"wall {report.wall_seconds:.1f} s, {report.runs_per_second:.2f} runs/s",
        f"  statuses {report.statuses}",
        "  run latency (created -> finished, includes the driver's approval/reconcile rounds): "
        + ", ".join(f"{k} {fmt(v)}" for k, v in report.latency_seconds.items()),
        "  job pickup latency: "
        + ", ".join(f"{k} {fmt(v)}" for k, v in report.pickup_seconds.items()),
        f"  job attempts {report.job_attempts}",
        f"  crashes {report.crashes}, dead jobs {report.dead_jobs}, stuck {report.stuck_runs}",
        f"  effects expected {report.effects_expected} actual {report.effects_actual}",
        f"  correctness: {'OK' if not violations(report) else violations(report)}",
    ]
    return "\n".join(lines) + "\n"


def write_results(report: Report, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = (
        directory
        / f"perf-{report.variant}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
    )
    path.write_text(json.dumps(asdict(report), indent=2, default=str), encoding="utf-8")
    return path


def machine() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "note": "workers, api and target share one event loop; Postgres is a local container",
    }


# --- the load -----------------------------------------------------------------------------------


class Load:
    """One load test: an API app, a fleet of workers and the runs they drain."""

    def __init__(
        self, env: Env, params: Params, fleet: Fleet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.env, self.params, self.fleet = env, params, fleet
        self.provider = LoadProvider(env.tmp_path, fleet)
        self.workload = load_workload(fleet)
        self.runtimes: list[WorkerRuntime] = []
        self.client: httpx.AsyncClient
        self.runs: dict[uuid.UUID, str] = {}  # run id -> scenario
        self.pickups: list[float] = []
        self.leased: dict[Any, uuid.UUID] = {}  # task -> lease token of its current job
        self.started: float | None = None
        self.wall = 0.0
        self.stack = AsyncExitStack()
        real = queue.lease_one

        async def recording(engine: Any, *, kinds: Sequence[str], lease_seconds: float) -> Any:
            job = await real(engine, kinds=kinds, lease_seconds=lease_seconds)
            if job is not None:
                self.leased[asyncio.current_task()] = job.lease_token
                self.pickups.append(job.waited_seconds)
            return job

        monkeypatch.setattr(queue, "lease_one", recording)
        (env.tmp_path / "read.yaml").write_text(SCENARIO_READ, encoding="utf-8")

    async def __aenter__(self) -> Load:
        app = create_app(self.env.settings())
        await self.stack.enter_async_context(app.router.lifespan_context(app))
        self.client = await self.stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        )
        self.runtimes = [await self._open_worker() for _ in range(self.params.workers)]
        return self

    async def __aexit__(self, *exc: object) -> None:
        for runtime in self.runtimes:
            await runtime.close()
        await self.stack.aclose()

    async def _open_worker(self) -> WorkerRuntime:
        runtime = await self.env.worker(self.provider)
        runtime.registries[WORKLOAD.name] = ToolRegistry(self.workload)
        return runtime

    # -- submitting and deciding (through the api) --

    async def submit(self, scenarios: Sequence[str]) -> list[uuid.UUID]:
        gate = asyncio.Semaphore(16)

        async def one(scenario: str) -> uuid.UUID:
            async with gate:
                response = await self.client.post(
                    "/v1/tasks/runs",
                    json={
                        "dev_identity": {
                            "workspace_id": WS,
                            "agent_id": AGENT,
                            "requester": {"sub": "alice", "roles": ["operator"]},
                        },
                        "task_input": {"message": f"load #scenario={scenario}"},
                    },
                )
            assert response.status_code == 201, response.text
            return uuid.UUID(response.json()["run_id"])

        ids = await asyncio.gather(*(one(s) for s in scenarios))
        self.runs.update(zip(ids, scenarios, strict=True))
        return list(ids)

    async def _post(self, url: str, body: dict[str, Any]) -> None:
        response = await self.client.post(url, json=body)
        assert response.status_code == 200, f"{url}: {response.status_code} {response.text}"

    async def decide(self, *, reconcile: bool) -> int:
        """Approve every pending approval (as a second identity); optionally reconcile every
        indeterminate entry as ``applied`` (as a third). Returns how many decisions were made."""
        mine = list(self.runs)
        approvals = self.env.sql(
            "SELECT id FROM tether.approvals WHERE status = 'pending' AND run_id = ANY(%s)", mine
        )
        entries = (
            self.env.sql(
                "SELECT l.id FROM tether.ledger_entries l JOIN tether.runs r ON r.id = l.run_id"
                " WHERE l.state = 'indeterminate' AND r.status = 'awaiting_reconciliation'"
                " AND l.run_id = ANY(%s)",
                mine,
            )
            if reconcile
            else []
        )
        gate = asyncio.Semaphore(16)

        async def limited(url: str, body: dict[str, Any]) -> None:
            async with gate:
                await self._post(url, body)

        approve = {"dev_identity": {"workspace_id": WS, "sub": "bob", "roles": ["approver"]}}
        resolve = {
            "dev_identity": {"workspace_id": WS, "sub": "dave", "roles": ["reconciler"]},
            "outcome": "applied",
            "justification": "checked the target",
        }
        await asyncio.gather(
            *(
                limited(f"/v1/approvals/{a}/approve", {**approve, "reason": "load approval"})
                for (a,) in approvals
            ),
            *(limited(f"/v1/ops/ledger/{e}/reconcile", resolve) for (e,) in entries),
        )
        return len(approvals) + len(entries)

    # -- draining with killable workers --

    async def _slot(self, index: int) -> int:
        done = 0
        while True:
            try:
                ran = await process_next_job(self.runtimes[index])
            except BaseException as error:
                if not is_crash(error):
                    raise
                await self._replace(index, asyncio.current_task())
                continue
            if not ran:
                return done
            done += 1

    async def _replace(self, index: int, task: Any) -> None:
        """The worker died: its lease runs out and a fresh process takes its place."""
        token = self.leased.get(task)
        if token is not None:
            self.env.sql(
                "UPDATE tether.jobs SET lease_expires_at = clock_timestamp() - interval '1 second'"
                " WHERE lease_token = %s RETURNING id",
                token,
            )
        await self.runtimes[index].close()
        self.runtimes[index] = await self._open_worker()

    def _unsettled(self, *, reconcile: bool) -> list[tuple[uuid.UUID, str]]:
        rows = self.env.sql(
            "SELECT id, status FROM tether.runs WHERE id = ANY(%s)", list(self.runs)
        )
        held = set() if reconcile else {"awaiting_reconciliation"}
        return [(i, s) for i, s in rows if s not in TERMINAL and s not in held]

    async def drive(self, *, reconcile: bool = False) -> None:
        """Drain, decide, repeat until every run is terminal (or, without ``reconcile``, held for
        reconciliation). Waits on database state; fails with the stuck runs after the timeout."""
        begin = time.monotonic()
        self.started = self.started or begin
        deadline = begin + self.params.timeout_seconds
        try:
            while True:
                done = sum(
                    await asyncio.gather(*(self._slot(i) for i in range(len(self.runtimes))))
                )
                decided = await self.decide(reconcile=reconcile)
                unsettled = self._unsettled(reconcile=reconcile)
                if not unsettled:
                    return
                if time.monotonic() > deadline:
                    raise AssertionError(
                        f"{len(unsettled)} run(s) unsettled after {self.params.timeout_seconds}s: "
                        f"{unsettled[:5]}"
                    )
                if not done and not decided:
                    await asyncio.sleep(0.05)  # only contended retries are due later
        finally:
            self.wall += time.monotonic() - begin

    # -- results --

    async def collect(
        self, variant: str, expected: dict[str, int], effects_expected: dict[str, int]
    ) -> Report:
        ids = list(self.runs)
        rows = self.env.sql(
            "SELECT status, count(*) FROM tether.runs WHERE id = ANY(%s) GROUP BY 1", ids
        )
        statuses = {str(status): int(n) for status, n in rows}
        stuck = sum(
            n for s, n in statuses.items() if s not in TERMINAL and s != "awaiting_reconciliation"
        )
        latency = [
            float(x)
            for (x,) in self.env.sql(
                "SELECT extract(epoch FROM (e.created_at - r.created_at))"
                " FROM tether.runs r JOIN tether.audit_events e"
                " ON e.run_id = r.id AND e.dedupe_key = 'run_finished' WHERE r.id = ANY(%s)",
                ids,
            )
        ]
        attempts = {
            f"{kind}:{n_attempts}": int(n)
            for kind, n_attempts, n in self.env.sql(
                "SELECT kind, attempts, count(*) FROM tether.jobs GROUP BY 1, 2 ORDER BY 1, 2"
            )
        }
        count = lambda state: int(  # noqa: E731
            self.env.sql("SELECT count(*) FROM tether.jobs WHERE state = %s", state)[0][0]
        )
        invalid = 0
        for run_id in ids:
            response = await self.client.get(
                f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WS}
            )
            assert response.status_code == 200, response.text
            invalid += 0 if response.json()["audit_chain_valid"] is True else 1
        write_keys = {
            str(k)
            for (k,) in self.env.sql(
                "SELECT idempotency_key FROM tether.ledger_entries"
                " WHERE tool = 'probe_write' AND state = 'completed' AND run_id = ANY(%s)",
                ids,
            )
        }
        actual = {
            "probe_write": len(self.fleet.effects) if self.fleet.effects == write_keys else -1,
            "probe_nonidem": self.fleet.nonidem_effects,
            "restart_service": await restarts(),
        }
        return Report(
            variant=variant,
            params={k: str(v) for k, v in asdict(self.params).items()},
            submitted=len(ids),
            expected=expected,
            statuses=statuses,
            wall_seconds=self.wall,
            latency_seconds={
                "p50": percentile(latency, 0.50),
                "p95": percentile(latency, 0.95),
                "p99": percentile(latency, 0.99),
            },
            pickup_seconds={
                "p50": percentile(self.pickups, 0.50),
                "p95": percentile(self.pickups, 0.95),
            },
            job_attempts=attempts,
            leased_jobs=count("leased"),
            dead_jobs=count("dead"),
            stuck_runs=stuck,
            effects_expected=effects_expected,
            effects_actual=actual,
            chains_invalid=invalid,
            crashes=dict(self.fleet.crashes),
            machine=machine(),
        )

    async def check_samples(self) -> None:
        """T-12 v0 and terminal stability on a sample of finished runs."""
        terminal = [
            run_id
            for run_id, _ in self.env.sql(
                "SELECT id, status FROM tether.runs WHERE id = ANY(%s)", list(self.runs)
            )
            if self.env.status(run_id) in TERMINAL
        ]
        step = max(1, len(terminal) // max(1, self.params.sample))
        picked = terminal[::step][: self.params.sample]
        for run_id in picked:
            await self.env.assert_reconstructable(run_id)
        for run_id in picked[:3]:
            await self.env.assert_terminal_stable(run_id, self.runtimes[0])

    def finish(self, report: Report) -> None:
        sys.stdout.write(render(report))
        sys.stdout.write(f"  results file: {write_results(report, self.params.results_dir)}\n")
        assert violations(report) == []
