"""Stop requests: cancel (TX9) and deadline (TX10), §15 and ADR-0006.

Both run under the run row's ``FOR UPDATE`` lock (taken by the caller) and record a request, never
a status: they set the stop flag, close the run's pending approvals, audit each change and wake the
run, all in the caller's transaction. The run driver then honours the stop at its next gate. Both
are no-ops on a terminal run or when their flag is already set, so repeats change nothing.
"""

from __future__ import annotations

from sqlalchemy.engine import RowMapping

from tether.core.statuses import TERMINAL_RUN_STATUSES, JobKind
from tether.secrets.keyring import KeyRing
from tether.storage.repos.approvals import ApprovalsRepository
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.runs import JobsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork


async def request_cancel(
    uow: UnitOfWork, keys: KeyRing, scope: WorkspaceScope, run: RowMapping, *, by: str
) -> bool:
    """TX9. ``run`` is the row locked ``FOR UPDATE``. Returns whether anything changed."""
    if run["status"] in TERMINAL_RUN_STATUSES or run["cancel_requested_at"] is not None:
        return False
    await uow.repository(RunsRepository, scope).request_cancel(run["id"], by)
    await _stop(uow, keys, scope, run, event="CANCEL_REQUESTED", actor=by, reason="cancelled")
    return True


async def record_deadline(
    uow: UnitOfWork, keys: KeyRing, scope: WorkspaceScope, run: RowMapping
) -> bool:
    """TX10. ``run`` is the row locked ``FOR UPDATE``. Returns whether anything changed."""
    if (
        run["status"] in TERMINAL_RUN_STATUSES
        or run["deadline_exceeded_at"] is not None
        or not run["deadline_reached"]
    ):
        return False
    await uow.repository(RunsRepository, scope).record_deadline_exceeded(run["id"])
    await _stop(uow, keys, scope, run, event="DEADLINE_EXCEEDED", actor=None, reason="deadline")
    return True


async def _stop(
    uow: UnitOfWork,
    keys: KeyRing,
    scope: WorkspaceScope,
    run: RowMapping,
    *,
    event: str,
    actor: str | None,
    reason: str,
) -> None:
    # Lock order: run row (held) → approval rows → audit head → jobs (§13.1).
    closed = await uow.repository(ApprovalsRepository, scope).close_pending(run["id"])
    audit = uow.repository(AuditRepository, scope)
    await audit.append(
        keys,
        run_id=run["id"],
        dedupe_key=event.lower(),
        event_type=event,
        actor=actor,
        payload={"deadline_at": run["deadline_at"].isoformat()} if actor is None else {},
    )
    for approval in closed:
        await audit.append(
            keys,
            run_id=run["id"],
            dedupe_key=f"approval_closed:{approval['id']}",
            event_type="APPROVAL_CLOSED",
            actor=actor,
            payload={"approval_id": str(approval["id"]), "reason": reason},
            tool=approval["tool"],
        )
    await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, run["id"])
