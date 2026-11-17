"""T-19 / G-15 (strategy §4.1 item 4): the approval decision route under a stale policy.

``POST /v1/approvals/{id}/{action}`` with real credentials, against an api process that loads a
policy version other than the workspace's active one: ``503`` for both actions, and nothing
changes (no approval row, audit event, job or ledger entry). The same approval stays decidable
by a process that holds the active version. The dev-identity variant of the approve case is
``test_policy_permissions.py::test_g15_the_approval_route_answers_503_*``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from tests.integration.evidence_support import (
    BOOT_SENSITIVE,
    RecordingProvider,
    approval_id,
    bob,
    call,
    rows,
    run_status,
    submit,
)
from tests.integration.evidence_support import (
    demo_target as demo_target,
)
from tests.integration.evidence_support import (
    provider as provider,
)
from tests.integration.evidence_support import (
    runtime as runtime,
)
from tests.integration.sink_scan import table_sinks

from tether.cli.bootstrap import apply_bootstrap
from tether.policy.config import BootstrapFile
from tether.runtime.driver import WorkerRuntime

ACTIONS = ("approve", "reject")


def snapshot(seeded: Any) -> dict[str, str]:
    """Every row that a decision could touch, as text, so any change shows."""
    found = table_sinks(seeded.db.migrator_dsn)
    return {
        table: found[f"table:tether.{table}"]
        for table in ("approvals", "audit_events", "jobs", "ledger_entries", "runs")
    }


def newer_policy() -> BootstrapFile:
    changed = copy.deepcopy(BOOT_SENSITIVE.model_dump(mode="json"))
    changed["workspaces"][0]["policy"]["reconciliation"]["reconciler_roles"] = ["sre", "lead"]
    return BootstrapFile.model_validate(changed)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.asyncio
async def test_t19_g15_a_decision_under_a_stale_policy_is_503_and_changes_nothing(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    provider: RecordingProvider,
    action: str,
) -> None:
    run_id = await submit(runtime, seeded, tmp_path, "restart")
    found = approval_id(seeded, run_id)
    newer = newer_policy()
    apply_bootstrap(seeded.db.migrator_dsn, newer)  # the active version moves on; the api's has not
    before = snapshot(seeded)

    response = await call(
        seeded,
        tmp_path,
        "POST",
        f"/v1/approvals/{found}/{action}",
        headers=bob(),
        json={"reason": "stale"},
    )

    assert response.status_code == 503, response.text
    assert snapshot(seeded) == before
    assert rows(seeded, "SELECT status FROM tether.approvals WHERE id = %s", found) == [
        ("pending",)
    ]
    assert run_status(seeded, run_id) == "awaiting_approval"

    # Nothing was consumed: a process that holds the active version still decides it.
    decided = await call(
        seeded,
        tmp_path,
        "POST",
        f"/v1/approvals/{found}/{action}",
        config=newer,
        headers=bob(),
        json={"reason": "current"},
    )
    assert decided.status_code == 200, decided.text
    assert rows(seeded, "SELECT status FROM tether.approvals WHERE id = %s", found) == [
        ("approved" if action == "approve" else "rejected",)
    ]
