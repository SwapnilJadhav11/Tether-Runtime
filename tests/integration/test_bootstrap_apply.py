"""MVP M1: ``tether bootstrap apply`` (ADR-0014, ADR-0009: one active policy version)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from tether.cli.main import app

runner = CliRunner()

CONFIG = """
workspaces:
  - id: ws-boot
    agents:
      - id: agent-boot
        workload: probe
    policy:
      rules:
        - tool: read_thing
          decision: allow
"""


def _apply(fresh_db: Any, tmp_path: Path, text: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "bootstrap.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("TETHER_MIGRATOR_DATABASE_URL", fresh_db.migrator_dsn)
    return runner.invoke(app, ["bootstrap", "apply", str(path)])


def _state(fresh_db: Any) -> tuple[Any, ...]:
    with psycopg.connect(fresh_db.migrator_dsn) as conn:
        return (
            conn.execute("SELECT id FROM tether.workspaces ORDER BY id").fetchall(),
            conn.execute("SELECT workspace_id, agent_id FROM tether.agents").fetchall(),
            conn.execute(
                "SELECT workspace_id, version, active FROM tether.policy_versions"
                " ORDER BY created_at"
            ).fetchall(),
        )


def test_apply_creates_workspace_agent_and_active_policy(
    fresh_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _apply(fresh_db, tmp_path, CONFIG, monkeypatch)
    assert result.exit_code == 0, result.output
    workspaces, agents, policies = _state(fresh_db)
    assert workspaces == [("ws-boot",)]
    assert agents == [("ws-boot", "agent-boot")]
    assert len(policies) == 1
    assert policies[0][0] == "ws-boot"
    assert policies[0][2] is True


def test_applying_twice_changes_nothing(
    fresh_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _apply(fresh_db, tmp_path, CONFIG, monkeypatch).exit_code == 0
    first = _state(fresh_db)
    assert _apply(fresh_db, tmp_path, CONFIG, monkeypatch).exit_code == 0
    assert _state(fresh_db) == first


def test_a_policy_change_activates_a_new_version(
    fresh_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _apply(fresh_db, tmp_path, CONFIG, monkeypatch).exit_code == 0
    changed = CONFIG.replace("decision: allow", "decision: deny")
    assert _apply(fresh_db, tmp_path, changed, monkeypatch).exit_code == 0
    policies = _state(fresh_db)[2]
    assert [active for _, _, active in policies] == [False, True]
    assert policies[0][1] != policies[1][1]


def test_an_invalid_file_fails_and_leaves_the_database_untouched(
    fresh_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _state(fresh_db)
    result = _apply(fresh_db, tmp_path, CONFIG.replace("decision: allow", "x: 1"), monkeypatch)
    assert result.exit_code != 0
    assert _state(fresh_db) == before
