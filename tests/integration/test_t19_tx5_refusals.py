"""P-16, T-19 / T-02 (integration): the TX5 refusal branches of ``graph.py::_authorize``.

TX5 re-checks, under the current policy, the call that was decided (and approved) earlier. Each
refusal must leave no ledger entry, send nothing, hand the model a typed result, and let the run
carry on. The "policy now: deny" branch is proven elsewhere
(``test_policy_permissions.py::test_t06_t19_a_permission_revoked_after_approval_is_refused_at_tx5``)
and is not repeated here.

Three branches cannot be reached by a legitimate state change alone, because an earlier node
re-reads the same row and fails closed first (``await_approval`` recomputes the stored hash and
turns a mismatch into ``REPLAY_DIVERGENCE``; nothing moves an approved approval back). They are
exercised by changing the one input TX5 reads, at the moment TX5 reads it: the test wraps
``_assert_current_policy`` (the call immediately before TX5's own checks) and then edits the
approval row, or has the next hash recomputation differ. The product code is untouched.
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

import pytest
from tests.integration.evidence_support import (
    BOOT_SENSITIVE,
    SENSITIVE_WORKLOAD,
    CountingTransport,
    RecordingProvider,
    approval_id,
    approve,
    drain,
    rows,
    run_status,
    settings_for,
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

from tether.cli.bootstrap import apply_bootstrap
from tether.core.statuses import JobKind
from tether.policy.config import BootstrapFile
from tether.registry.registry import ToolRegistry
from tether.runtime import graph
from tether.runtime.driver import WorkerRuntime
from tether.worker.main import process_next_job


def tool_results(provider: RecordingProvider) -> list[tuple[str, str]]:
    """(outcome, content) of every tool message in the last history the model was given."""
    return [
        (str(message["outcome"]), str(message["content"]))
        for message in provider.histories[-1]
        if message["role"] == "tool"
    ]


def events(seeded: Any, run_id: uuid.UUID) -> list[str]:
    found = rows(
        seeded, "SELECT event_type FROM tether.audit_events WHERE run_id = %s ORDER BY seq", run_id
    )
    return [event for (event,) in found]


def assert_nothing_authorized_or_sent(
    seeded: Any, run_id: uuid.UUID, demo_target: CountingTransport
) -> None:
    assert rows(seeded, "SELECT count(*) FROM tether.ledger_entries WHERE run_id = %s", run_id) == [
        (0,)
    ]
    assert rows(
        seeded,
        "SELECT count(*) FROM tether.jobs WHERE run_id = %s AND kind = 'execute_tool'",
        run_id,
    ) == [(0,)]
    assert "EXECUTION_AUTHORIZED" not in events(seeded, run_id)
    assert not demo_target.requests  # no request reached the target


async def approved_and_waiting(
    runtime: WorkerRuntime, seeded: Any, tmp_path: Path
) -> tuple[uuid.UUID, uuid.UUID]:
    """A ``restart_service`` run whose approval was granted but not yet acted on."""
    run_id = await submit(runtime, seeded, tmp_path, "restart")
    assert run_status(seeded, run_id) == "awaiting_approval"
    found = approval_id(seeded, run_id)
    assert (await approve(seeded, tmp_path, found)).status_code == 200
    return run_id, found


class OnPolicyCheck:
    """Wraps ``graph._assert_current_policy`` so a test acts once, right after the check that
    precedes TX5's own refusal checks, and only after ``arm()``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, action: Any) -> None:
        self._armed = False
        self._action = action
        original = graph._assert_current_policy

        async def wrapped(uow: Any, ctx: Any) -> None:
            await original(uow, ctx)
            if self._armed:
                self._armed = False
                self._action()

        monkeypatch.setattr(graph, "_assert_current_policy", wrapped)

    def arm(self) -> None:
        self._armed = True


# --- approval lapsed after it was approved ---------------------------------------------------


@pytest.mark.asyncio
async def test_t19_p16_an_approval_that_lapsed_after_it_was_granted_is_refused_at_tx5(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    provider: RecordingProvider,
    demo_target: CountingTransport,
) -> None:
    run_id, found = await approved_and_waiting(runtime, seeded, tmp_path)
    # The approval was decided while live; its expiry then passes before the run acts on it.
    assert rows(
        seeded,
        "UPDATE tether.approvals SET expires_at = clock_timestamp() - interval '1 minute'"
        " WHERE id = %s RETURNING 1",
        found,
    ) == [(1,)]
    await drain(runtime)
    assert_nothing_authorized_or_sent(seeded, run_id, demo_target)
    # The approval itself stays what it was: approved, never rewritten to hide the lapse.
    assert rows(seeded, "SELECT status FROM tether.approvals WHERE id = %s", found) == [
        ("approved",)
    ]
    # The model is told the approval lapsed, with the expired outcome, and the run carries on.
    assert tool_results(provider) == [("expired", "approval lapsed")]
    assert run_status(seeded, run_id) == "succeeded"
    assert "RUN_FINISHED" in events(seeded, run_id)


# --- no matching approval --------------------------------------------------------------------


def _close(seeded: Any, found: uuid.UUID) -> Any:
    def act() -> None:
        rows(
            seeded,
            "UPDATE tether.approvals SET status = 'pending', decided_by = NULL, decided_at = NULL,"
            " decision_reason = NULL, policy_version = NULL WHERE id = %s RETURNING 1",
            found,
        )

    return act


def _rehash(seeded: Any, found: uuid.UUID) -> Any:
    def act() -> None:
        rows(
            seeded,
            "UPDATE tether.approvals SET call_hash = repeat('f', 64) WHERE id = %s RETURNING 1",
            found,
        )

    return act


def _remove(seeded: Any, found: uuid.UUID) -> Any:
    def act() -> None:
        rows(seeded, "DELETE FROM tether.approvals WHERE id = %s RETURNING 1", found)

    return act


@pytest.mark.parametrize(
    "disturb",
    [_close, _rehash, _remove],
    ids=["not-approved-any-more", "bound-to-another-hash", "no-approval-row"],
)
@pytest.mark.asyncio
async def test_t19_p16_an_approval_that_no_longer_matches_the_call_is_refused_at_tx5(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    provider: RecordingProvider,
    demo_target: CountingTransport,
    monkeypatch: pytest.MonkeyPatch,
    disturb: Any,
) -> None:
    run_id = await submit(runtime, seeded, tmp_path, "restart")
    found = approval_id(seeded, run_id)
    hook = OnPolicyCheck(monkeypatch, disturb(seeded, found))
    assert (await approve(seeded, tmp_path, found)).status_code == 200
    hook.arm()
    await drain(runtime)
    assert_nothing_authorized_or_sent(seeded, run_id, demo_target)
    assert tool_results(provider) == [("denied", "no matching approval")]
    assert run_status(seeded, run_id) == "succeeded"


# --- the call changed since its decision -----------------------------------------------------


@pytest.mark.asyncio
async def test_t19_p16_a_call_whose_hash_differs_from_its_decision_is_refused_at_tx5(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    provider: RecordingProvider,
    demo_target: CountingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A read: allowed by policy, so the only thing that can refuse it at TX5 is the hash check.
    original = graph.call_hash_for
    armed = {"on": False}

    real_check = graph._assert_current_policy
    checks = {"n": 0}

    async def arm_after_check(uow: Any, ctx: Any) -> None:
        await real_check(uow, ctx)
        checks["n"] += 1
        # The first check is TX2's (the decision keeps the true hash); the second is TX5's, and
        # the next hash recomputation is TX5's own.
        armed["on"] = checks["n"] == 2

    def drifted(*args: Any, **kwargs: Any) -> str:
        found: str = original(*args, **kwargs)
        if armed["on"]:
            armed["on"] = False
            return found[:-1] + ("0" if found[-1] != "0" else "1")
        return found

    monkeypatch.setattr(graph, "_assert_current_policy", arm_after_check)
    monkeypatch.setattr(graph, "call_hash_for", drifted)
    run_id = await submit(runtime, seeded, tmp_path, "read")
    assert_nothing_authorized_or_sent(seeded, run_id, demo_target)
    assert tool_results(provider) == [("denied", "call changed since its decision")]
    assert run_status(seeded, run_id) == "succeeded"
    # The decision that was recorded is untouched: the refusal rewrote nothing.
    assert "POLICY_DECISION" in events(seeded, run_id)


# --- a stale policy version at TX5 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_g15_p16_a_stale_tx5_records_nothing_and_a_current_worker_then_proceeds(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    provider: RecordingProvider,
    demo_target: CountingTransport,
) -> None:
    run_id, _ = await approved_and_waiting(runtime, seeded, tmp_path)
    # A deployment changes the policy (here only a reconciler role, which does not touch this
    # call). The old worker still holds the previous version.
    changed = copy.deepcopy(BOOT_SENSITIVE.model_dump(mode="json"))
    changed["workspaces"][0]["policy"]["reconciliation"]["reconciler_roles"] = ["sre", "lead"]
    newer = BootstrapFile.model_validate(changed)
    apply_bootstrap(seeded.db.migrator_dsn, newer)
    assert newer.workspaces[0].policy_version != BOOT_SENSITIVE.workspaces[0].policy_version

    assert await process_next_job(runtime, kinds=(JobKind.ADVANCE_RUN,))
    assert_nothing_authorized_or_sent(seeded, run_id, demo_target)
    # Nothing was recorded, and the wake was put back untouched for a worker that is current.
    assert rows(
        seeded,
        "SELECT state, attempts FROM tether.jobs WHERE run_id = %s AND kind = 'advance_run'"
        " AND state <> 'done'",
        run_id,
    ) == [("ready", 0)]
    assert run_status(seeded, run_id) == "running"
    assert len(provider.histories) == 1  # the stale worker did not go back to the model

    # The requeue carries a short backoff; make it due now instead of sleeping through it.
    assert rows(
        seeded,
        "UPDATE tether.jobs SET run_after = clock_timestamp() WHERE run_id = %s"
        " AND kind = 'advance_run' AND state = 'ready' RETURNING 1",
        run_id,
    ) == [(1,)]
    fresh = await WorkerRuntime.open(
        settings_for(seeded.db, tmp_path, newer), bootstrap=newer, provider=provider
    )
    fresh.registries[SENSITIVE_WORKLOAD.name] = ToolRegistry(SENSITIVE_WORKLOAD)
    try:
        await drain(fresh)
    finally:
        await fresh.close()
    # Under the current version the same approved call is authorized and executed once.
    assert rows(
        seeded, "SELECT tool, state FROM tether.ledger_entries WHERE run_id = %s", run_id
    ) == [("restart_service", "completed")]
    assert len(demo_target.requests) == 1
    assert run_status(seeded, run_id) == "succeeded"
    assert tool_results(provider)[0][0] == "applied"
