"""Timer jobs (§11.4): ``run_deadline`` (TX10) and ``expire_approval`` (TX-EA).

A timer only materialises state the gates already enforce at decision time (the stored deadline
and ``expires_at`` are compared with ``clock_timestamp()`` wherever a decision is made). Each is an
idempotent conditional update under the run row's ``FOR UPDATE`` lock that re-checks the current
state, writes its audit event and wakes the run; on a terminal run, or when already applied, it is
a no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tether.core.statuses import TERMINAL_RUN_STATUSES, JobKind
from tether.observability import metrics
from tether.observability.logging import get_logger
from tether.queue.postgres import LeasedJob
from tether.runtime import stop_requests
from tether.runtime.driver import WorkerRuntime
from tether.storage.repos.approvals import ApprovalsRepository
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.runs import JobsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

_log = get_logger(__name__)


def _scope(job: LeasedJob) -> WorkspaceScope:
    return WorkspaceScope.for_job(
        job_workspace_id=job.workspace_id, run_workspace_id=job.run_workspace_id
    )


async def run_deadline(runtime: WorkerRuntime, job: LeasedJob) -> None:
    """TX10: record ``deadline_exceeded_at``, close pending approvals, audit, wake the run."""
    scope = _scope(job)
    async with UnitOfWork(runtime.engine) as uow:
        run = await uow.repository(RunsRepository, scope).lock(job.run_id)
        if run is not None:
            await stop_requests.record_deadline(uow, runtime.keys, scope, run)


async def expire_approval(runtime: WorkerRuntime, job: LeasedJob) -> None:
    """TX-EA: ``pending → expired`` once lapsed, audited, and wake the run."""
    if job.ref_id is None:
        return
    scope = _scope(job)
    async with UnitOfWork(runtime.engine) as uow:
        # Lock order: run row → approval row → audit head → jobs (§13.1).
        run = await uow.repository(RunsRepository, scope).lock(job.run_id)
        if run is None or run["status"] in TERMINAL_RUN_STATUSES:
            return
        approvals = uow.repository(ApprovalsRepository, scope)
        approval = await approvals.lock(job.ref_id)
        if approval is None or not await approvals.expire(job.ref_id):
            return
        await uow.repository(AuditRepository, scope).append(
            runtime.keys,
            run_id=job.run_id,
            dedupe_key=f"approval_expired:{job.ref_id}",
            event_type="APPROVAL_EXPIRED",
            payload={"approval_id": str(job.ref_id)},
            tool=approval["tool"],
        )
        await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, job.run_id)
        waited = (datetime.now(UTC) - approval["created_at"]).total_seconds()
        tool = str(approval["tool"])

        def expired() -> None:
            metrics.approval_resolved("expired", waited)
            _log.info("approval expired", tool=tool)

        uow.on_commit(expired)


class _StaleLease(Exception):  # noqa: N818 - a control-flow signal that rolls TX12 back
    """Another lessee owns the job now: nothing may commit."""


async def dead_letter(runtime: WorkerRuntime, job: LeasedJob) -> None:
    """An exhausted timer: marked ``dead`` with a wake, in one transaction. Its condition is still
    enforced at decision time, and the wake lets the run driver re-check the run."""
    scope = _scope(job)
    try:
        async with UnitOfWork(runtime.engine) as uow:
            jobs = uow.repository(JobsRepository, scope)
            if not await jobs.mark_dead(job.id, job.lease_token):
                raise _StaleLease
            await jobs.enqueue(JobKind.ADVANCE_RUN, job.run_id)
            uow.on_commit(lambda: metrics.job_dead_lettered(job.kind))
    except _StaleLease:
        return
