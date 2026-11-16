"""Negative self-tests: the load checks must fail when a correctness property is broken. A check
that cannot fail proves nothing (criterion 2)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from tests.perf.load import Params, Report, percentile, violations

pytestmark = pytest.mark.perf


def good() -> Report:
    return Report(
        variant="x",
        params={},
        submitted=4,
        expected={"succeeded": 4},
        statuses={"succeeded": 4},
        wall_seconds=1.0,
        latency_seconds={},
        pickup_seconds={},
        job_attempts={},
        leased_jobs=0,
        dead_jobs=0,
        stuck_runs=0,
        effects_expected={"probe_write": 4},
        effects_actual={"probe_write": 4},
        chains_invalid=0,
        crashes={},
        machine={},
    )


def test_p02_a_correct_report_has_no_violations() -> None:
    assert violations(good()) == []


@pytest.mark.parametrize(
    "change",
    [
        {"statuses": {"succeeded": 3, "failed": 1}},
        {"stuck_runs": 1},
        {"leased_jobs": 1},
        {"dead_jobs": 1},
        {"chains_invalid": 1},
        {"effects_actual": {"probe_write": 5}},  # a duplicated effect
        {"effects_actual": {"probe_write": 3}},  # a lost effect
        {"effects_actual": {}},
    ],
)
def test_p02_each_broken_property_is_reported(change: dict[str, Any]) -> None:
    assert violations(dataclasses.replace(good(), **change)) != []


def test_p02_percentiles_use_nearest_rank() -> None:
    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 0.50) == 50.0
    assert percentile(values, 0.95) == 95.0
    assert percentile(values, 0.99) == 99.0
    assert percentile([], 0.5) is None


@pytest.mark.asyncio
async def test_p02_a_real_duplicate_effect_is_caught(make_load: Any) -> None:
    async with make_load() as load:
        await load.submit(["write"] * 3)
        await load.drive()
        expected = {"probe_write": 3, "probe_nonidem": 0, "restart_service": 0}
        clean = await load.collect("self", {"succeeded": 3}, expected)
        assert violations(clean) == []
        load.fleet.effects.add("a-second-effect-for-one-run")  # the target applied one too many
        assert any(
            "probe_write" in v
            for v in violations(await load.collect("self", {"succeeded": 3}, expected))
        )


@pytest.mark.asyncio
async def test_p02_a_real_double_application_of_a_non_idempotent_write_is_caught(
    make_load: Any,
) -> None:
    async with make_load() as load:
        await load.submit(["nonidem"] * 2)
        await load.drive()
        expected = {"probe_write": 0, "probe_nonidem": 2, "restart_service": 0}
        assert violations(await load.collect("self", {"succeeded": 2}, expected)) == []
        load.fleet.nonidem_effects += 1
        assert any(
            "probe_nonidem" in v
            for v in violations(await load.collect("self", {"succeeded": 2}, expected))
        )


@pytest.mark.asyncio
async def test_p02_a_stuck_run_and_a_leaked_lease_are_caught(make_load: Any) -> None:
    async with make_load() as load:
        await load.submit(["read"] * 2)
        await load.drive()
        [stuck] = await load.submit(["read"])  # never driven: queued with a ready job
        load.env.sql(
            "UPDATE tether.jobs SET state = 'leased', lease_token = gen_random_uuid(),"
            " lease_expires_at = clock_timestamp() + interval '1 hour'"
            " WHERE run_id = %s AND kind = 'advance_run'"
            " RETURNING id",
            stuck,
        )
        report = await load.collect(
            "self",
            {"succeeded": 3},
            {"probe_write": 0, "probe_nonidem": 0, "restart_service": 0},
        )
        found = violations(report)
        assert report.stuck_runs == 1
        assert report.leased_jobs == 1
        assert any("stuck" in v for v in found)
        assert any("leased" in v for v in found)


@pytest.mark.asyncio
async def test_p02_drive_fails_with_the_stuck_runs_when_nothing_can_finish(
    make_load: Any, params: Params
) -> None:
    async with make_load() as load:
        load.params = dataclasses.replace(params, timeout_seconds=1.0)
        await load.submit(["remediate"])
        load.runtimes, closed = [], load.runtimes  # no workers at all
        try:
            with pytest.raises(AssertionError, match="unsettled"):
                await load.drive()
        finally:
            load.runtimes = closed
