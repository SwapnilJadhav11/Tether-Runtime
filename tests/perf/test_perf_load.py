"""Load variants (plan W10.3; PRD P-01..P-03). Every variant asserts correctness first: each run
reaches its expected status, the target saw exactly the expected effects, no job is left leased or
dead, every audit chain is valid. Measurements are printed and written to a JSON file."""

from __future__ import annotations

from typing import Any

import pytest
from tests.perf.load import Params

pytestmark = [pytest.mark.perf, pytest.mark.asyncio]


def split(total: int, parts: dict[str, float]) -> list[str]:
    """``total`` scenario names in the given proportions (the last part takes the remainder)."""
    names = list(parts)
    counts = {name: round(total * parts[name]) for name in names[:-1]}
    counts[names[-1]] = total - sum(counts.values())
    return [name for name in names for _ in range(counts[name])]


async def test_p02_concurrent_reads_all_succeed(make_load: Any, params: Params) -> None:
    async with make_load() as load:
        await load.submit(["read"] * params.runs)
        await load.drive()
        report = await load.collect(
            "reads",
            expected={"succeeded": params.runs},
            effects_expected={"probe_write": 0, "probe_nonidem": 0, "restart_service": 0},
        )
        await load.check_samples()
        load.finish(report)


async def test_p03_approval_volume_each_restart_applied_once(
    make_load: Any, params: Params
) -> None:
    async with make_load() as load:
        await load.submit(["remediate"] * params.runs)
        await load.drive()  # approves every pending approval as a second identity
        report = await load.collect(
            "approvals",
            expected={"succeeded": params.runs},
            effects_expected={
                "probe_write": 0,
                "probe_nonidem": 0,
                "restart_service": params.runs,
            },
        )
        await load.check_samples()
        load.finish(report)


async def test_p02_unknown_outcomes_are_held_then_reconciled(
    make_load: Any, params: Params
) -> None:
    unknown = max(1, round(params.runs * params.unknown_fraction))
    mix = split(params.runs, {"nonidem": unknown / params.runs, "write": 0.5, "read": 0.5})
    async with make_load(drop_nonidem=True) as load:
        await load.submit(mix)
        await load.drive()  # held: the unknown outcomes wait for a human
        held = load.env.sql(
            "SELECT count(*) FROM tether.runs WHERE status = 'awaiting_reconciliation'"
            " AND id = ANY(%s)",
            list(load.runs),
        )
        assert held == [(mix.count("nonidem"),)]
        await load.drive(reconcile=True)
        report = await load.collect(
            "unknown-reconciled",
            expected={"succeeded": params.runs},
            effects_expected={
                "probe_write": mix.count("write"),
                "probe_nonidem": mix.count("nonidem"),  # applied once each, never re-sent
                "restart_service": 0,
            },
        )
        await load.check_samples()
        load.finish(report)


async def test_p02_workers_killed_during_the_drain_lose_nothing(
    make_load: Any, params: Params
) -> None:
    mix = split(params.runs, {"read": 1 / 3, "write": 1 / 3, "remediate": 1 / 3})
    async with make_load(crash_probability=params.crash_probability) as load:
        await load.submit(mix)
        await load.drive()
        report = await load.collect(
            "kill",
            expected={"succeeded": params.runs},
            effects_expected={
                "probe_write": mix.count("write"),
                "probe_nonidem": 0,
                "restart_service": mix.count("remediate"),
            },
        )
        await load.check_samples()
        assert sum(report.crashes.values()) > 0, "no worker was killed: raise the probability"
        load.finish(report)
