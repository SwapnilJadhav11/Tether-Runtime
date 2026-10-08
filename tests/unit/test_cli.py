from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tether.cli.main import app

runner = CliRunner()


def test_cli_lists_migrate_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "migrate" in result.output


def test_migrate_fails_clearly_without_migrator_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TETHER_MIGRATOR_DATABASE_URL", raising=False)
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code != 0
    assert "TETHER_MIGRATOR_DATABASE_URL" in result.output
