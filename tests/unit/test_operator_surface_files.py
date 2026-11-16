"""Files operators and CI read.

T-24: the authorization matrix (``poe test-authz``) runs in CI next to the integration suite, on the
same test database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


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
