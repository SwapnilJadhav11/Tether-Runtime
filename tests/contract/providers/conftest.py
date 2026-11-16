"""Fixtures for the provider contract suite. The machinery lives in ``harness.py``."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contract.providers.harness import ProviderCase, openai_case, scripted_case


@pytest.fixture(params=["scripted", "openai"])
def case(request: pytest.FixtureRequest, tmp_path: Path) -> ProviderCase:
    """Every provider that must satisfy the contract."""
    return scripted_case(tmp_path) if request.param == "scripted" else openai_case()
