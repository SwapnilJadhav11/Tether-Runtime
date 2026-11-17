"""Lease heartbeat loop (W6.1, ADR-0004 rule 2, ADR-0005 rule 5), without a database."""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import SecretStr, ValidationError

from tether.settings import Settings
from tether.worker.main import LeaseLoss, keep_lease

DSN = SecretStr("postgresql://tether_app:x@127.0.0.1:1/tether")


def test_the_heartbeat_interval_defaults_to_a_third_of_the_lease() -> None:
    settings = Settings(database_url=DSN, env="test", worker_lease_seconds=30.0)
    assert settings.lease_heartbeat_interval == pytest.approx(10.0)


def test_an_explicit_heartbeat_interval_must_be_below_the_lease() -> None:
    ok = Settings(
        database_url=DSN, env="test", worker_lease_seconds=30.0, worker_lease_heartbeat_seconds=5.0
    )
    assert ok.lease_heartbeat_interval == 5.0
    for bad in (30.0, 45.0):
        with pytest.raises(ValidationError):
            Settings(
                database_url=DSN,
                env="test",
                worker_lease_seconds=30.0,
                worker_lease_heartbeat_seconds=bad,
            )


@pytest.mark.asyncio
async def test_t29_a_healthy_beat_never_ends_the_loop() -> None:
    beats = 0

    async def beat() -> bool:
        nonlocal beats
        beats += 1
        return True

    task = asyncio.create_task(
        keep_lease(beat, lease_seconds=0.5, interval_seconds=0.05, since=time.monotonic())
    )
    await asyncio.sleep(0.4)
    assert not task.done()
    assert beats >= 3
    task.cancel()


@pytest.mark.asyncio
async def test_t29_a_lost_lease_ends_the_loop_at_once() -> None:
    async def beat() -> bool:
        return False

    loss = await keep_lease(beat, lease_seconds=60.0, interval_seconds=0.05, since=time.monotonic())
    assert loss is LeaseLoss.LOST


@pytest.mark.asyncio
async def test_t29_an_unreachable_database_for_less_than_a_lease_is_survived() -> None:
    started = time.monotonic()
    calls = 0

    async def beat() -> bool:
        nonlocal calls
        calls += 1
        if time.monotonic() - started < 0.3:
            raise OSError("database unreachable")
        return True

    task = asyncio.create_task(
        keep_lease(beat, lease_seconds=1.0, interval_seconds=0.05, since=started)
    )
    await asyncio.sleep(0.8)
    assert not task.done()
    assert calls > 3
    task.cancel()


@pytest.mark.asyncio
async def test_t29_an_unreachable_database_for_a_full_lease_ends_the_loop() -> None:
    async def beat() -> bool:
        raise OSError("database unreachable")

    started = time.monotonic()
    loss = await keep_lease(beat, lease_seconds=0.4, interval_seconds=0.05, since=started)
    assert loss is LeaseLoss.UNREACHABLE
    assert 0.4 <= time.monotonic() - started < 1.5


@pytest.mark.asyncio
async def test_t29_a_beat_that_hangs_is_cut_off_at_the_lease_boundary() -> None:
    async def beat() -> bool:
        await asyncio.sleep(30)
        return True

    started = time.monotonic()
    loss = await keep_lease(beat, lease_seconds=0.4, interval_seconds=0.05, since=started)
    assert loss is LeaseLoss.UNREACHABLE
    assert time.monotonic() - started < 1.5
