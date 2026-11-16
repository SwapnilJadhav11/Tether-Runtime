"""P-17, T-16 (run level): a ``Sensitive`` argument and a ``Sensitive`` output through a real run.

``rotate_secret`` takes ``api_token`` (Sensitive) and returns ``new_token`` (Sensitive); the run
is submitted with real credentials, held for approval, approved, executed and finished. The raw
values (``CANARY_ARG``, ``CANARY_OUT``) must be absent, replaced by the mask, in each place
ADR-0017 says masked data lives: ``approvals.arguments_masked``,
``ledger_entries.outcome_payload_masked``, the ``APPROVAL_REQUESTED`` /
``EXECUTION_AUTHORIZED`` / ``TOOL_OUTCOME`` audit payloads, the
requester's inspection response, and the approver's list, detail and run view. The wider scan
across every sink is ``test_t16_sink_scan.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.integration.evidence_support import (
    CANARY_ARG,
    CANARY_OUT,
    alice,
    approval_id,
    approve,
    bob,
    call,
    drain,
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

from tether.registry.masking import MASK
from tether.runtime.driver import WorkerRuntime


def assert_no_raw_value(label: str, text: str) -> None:
    assert CANARY_ARG not in text, f"raw argument in {label}"
    assert CANARY_OUT not in text, f"raw output in {label}"


async def finished_run(runtime: WorkerRuntime, seeded: Any, tmp_path: Path) -> Any:
    run_id = await submit(runtime, seeded, tmp_path, "rotate")
    assert run_status(seeded, run_id) == "awaiting_approval"
    found = approval_id(seeded, run_id)
    assert (await approve(seeded, tmp_path, found)).status_code == 200
    await drain(runtime)
    assert run_status(seeded, run_id) == "succeeded"
    return run_id, found


@pytest.mark.asyncio
async def test_t16_p17_persisted_rows_hold_the_mask_never_the_raw_values(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id, found = await finished_run(runtime, seeded, tmp_path)

    # The approval request: the exact action, masked.
    [(arguments, preconditions)] = rows(
        seeded,
        "SELECT arguments_masked, preconditions_masked FROM tether.approvals WHERE id = %s",
        found,
    )
    assert arguments == {"service": "checkout", "api_token": MASK}
    assert preconditions == {}

    # The ledger: the tool's output as persisted, masked at the source.
    [(outcome, state, key)] = rows(
        seeded,
        "SELECT outcome_payload_masked, state, idempotency_key FROM tether.ledger_entries"
        " WHERE run_id = %s",
        run_id,
    )
    assert state == "completed"
    assert outcome["new_token"] == MASK
    assert outcome["status"] == "rotated"  # a non-sensitive field is kept
    assert outcome["service"] == "checkout"
    assert_no_raw_value("ledger outcome", json.dumps(outcome))
    assert_no_raw_value("idempotency key", key)

    # The audit chain: every payload of the run, and the three that carry the call.
    events = rows(
        seeded,
        "SELECT event_type, payload_masked FROM tether.audit_events WHERE run_id = %s ORDER BY seq",
        run_id,
    )
    by_type = dict(events)
    assert {"APPROVAL_REQUESTED", "EXECUTION_AUTHORIZED", "TOOL_OUTCOME"} <= set(by_type)
    for event, payload in events:
        assert_no_raw_value(f"{event} payload", json.dumps(payload))
    requested = by_type["APPROVAL_REQUESTED"]
    assert MASK in json.dumps(requested)
    assert requested["arguments"]["api_token"] == MASK
    # TOOL_OUTCOME records state and class, never the tool's output.
    assert set(by_type["TOOL_OUTCOME"]) == {"state", "reason", "ledger_id", "outcome_class"}

    # Every column of every approval and ledger row, not only the masked ones.
    for table in ("approvals", "ledger_entries"):
        text = rows(
            seeded,
            f"SELECT coalesce(string_agg(row_to_json(t)::text, ' '), '') FROM tether.{table} t",  # noqa: S608
        )[0][0]
        assert_no_raw_value(f"tether.{table}", text)


@pytest.mark.asyncio
async def test_t16_p17_the_requester_and_approver_views_show_the_mask_never_the_raw_values(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> None:
    run_id = await submit(runtime, seeded, tmp_path, "rotate")
    found = approval_id(seeded, run_id)
    urls = {
        "requester run": (f"/v1/tasks/runs/{run_id}", alice()),
        "approver list": ("/v1/approvals/approvals", bob()),
        "approver detail": (f"/v1/approvals/approvals/{found}", bob()),
        "approver run view": (f"/v1/approvals/runs/{run_id}", bob()),
    }

    async def read_all(stage: str) -> dict[str, Any]:
        views: dict[str, Any] = {}
        for label, (url, creds) in urls.items():
            response = await call(seeded, tmp_path, "GET", url, headers=creds)
            if stage == "after" and label == "approver list":
                assert response.json()["items"] == []  # decided: no longer pending
            assert response.status_code == 200, (stage, label, response.text)
            assert_no_raw_value(f"{stage} {label}", response.text)
            views[label] = response.json()
        return views

    # Pending: the approver is shown the masked exact action.
    pending = await read_all("before")
    [item] = pending["approver list"]["items"]
    assert item["arguments_masked"] == {"service": "checkout", "api_token": MASK}
    assert pending["approver detail"]["arguments_masked"] == item["arguments_masked"]
    assert pending["requester run"]["approvals"][0]["arguments_masked"] == item["arguments_masked"]
    assert (
        pending["approver run view"]["approvals"][0]["arguments_masked"] == item["arguments_masked"]
    )

    # Executed: the ledger outcome is in both run views, masked.
    assert (await approve(seeded, tmp_path, found)).status_code == 200
    await drain(runtime)
    assert run_status(seeded, run_id) == "succeeded"
    done = await read_all("after")
    for label in ("requester run", "approver run view"):
        [entry] = done[label]["ledger"]
        assert entry["outcome"]["new_token"] == MASK, label
        assert entry["outcome"]["status"] == "rotated", label
    assert done["approver detail"]["arguments_masked"] == item["arguments_masked"]
