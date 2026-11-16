"""Perf tests reuse the chaos fixtures (a migrated template cloned per test, an in-process demo
target, zero retry backoff) and add their parameters and a ``Load`` per test."""

from __future__ import annotations

from typing import Any

import pytest
from tests.chaos.conftest import demo_target, env
from tests.chaos.harness import Env
from tests.integration.conftest import fresh_db, rows, seeded, template_database
from tests.perf.load import Coin, Fleet, Load, Params

__all__ = ["demo_target", "env", "fresh_db", "rows", "seeded", "template_database"]


@pytest.fixture
def params() -> Params:
    return Params.from_env()


@pytest.fixture
def make_load(env: Env, params: Params, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``async with make_load(crash_probability=..., drop_nonidem=...) as load``."""

    def make(**fleet_options: Any) -> Load:
        return Load(env, params, Fleet(Coin(params.seed), **fleet_options), monkeypatch)

    return make
