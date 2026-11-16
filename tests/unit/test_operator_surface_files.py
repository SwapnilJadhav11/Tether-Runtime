"""Files operators and CI read: the CI jobs and the configuration examples.

T-24: the authorization matrix (``poe test-authz``) runs in CI next to the integration suite, on the
same test database. T-15: the approval-expiry maximum is named wherever an operator looks for a
setting: both environment examples and the production configuration guide.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
OPERATOR_FILES = (
    ROOT / "deploy" / "compose" / ".env.example",
    ROOT / "deploy" / "compose" / ".env.production.example",
    ROOT / "docs" / "operations" / "production-configuration.md",
)


def _run_steps(job: str) -> list[str]:
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = workflow["jobs"][job]["steps"]
    return [str(step["run"]).strip() for step in steps if "run" in step]


def test_t24_ci_runs_the_authorization_matrix_after_the_integration_suite() -> None:
    runs = _run_steps("integration")
    assert "uv run poe test-authz" in runs, runs
    assert runs.index("uv run poe test-authz") == runs.index("uv run poe test-integration") + 1
    # It needs the test database the job starts and must come before the job stops it.
    stop = next(i for i, run in enumerate(runs) if "docker-compose.test.yml down" in run)
    assert runs.index("uv run poe test-authz") < stop


@pytest.mark.parametrize("path", OPERATOR_FILES, ids=lambda path: path.name)
def test_t15_the_approval_expiry_settings_are_named_for_operators(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "TETHER_APPROVAL_TTL_SECONDS" in text
    assert "TETHER_APPROVAL_MAX_TTL_SECONDS" in text
