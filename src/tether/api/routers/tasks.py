"""``/v1/tasks`` (audience ``tether-tasks`` + workspace API key): submit a run (TX1), read it
back, and cancel it (TX9). Reading and cancelling are for the run's requester only (ADR-0013).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from psycopg import errors as pg_errors
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from tether.audit.chain import verify_run
from tether.authz.deps import RunReader, Submission, run_reader, submission, tasks_principal
from tether.core.ids import WorkspaceId
from tether.core.statuses import JobKind, RunStatus
from tether.identity.principal import Principal
from tether.runtime import stop_requests
from tether.secrets.keyring import KeyFamily
from tether.storage.repos.approvals import ApprovalsRepository
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import JobsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

router = APIRouter(prefix="/v1/tasks")


class SubmittedRun(BaseModel):
    run_id: uuid.UUID
    status: str


class ApprovalView(BaseModel):
    id: uuid.UUID
    step_ordinal: int
    tool: str
    status: str
    arguments_masked: dict[str, Any]
    decided_by: str | None
    expires_at: datetime


class LedgerView(BaseModel):
    step_ordinal: int
    tool: str
    effect: str
    state: str
    attempts: int
    outcome: dict[str, Any] | None
    outcome_reason: str | None
    resolution: str | None
    resolved_by: str | None


class AuditView(BaseModel):
    seq: int
    event_type: str
    actor: str | None
    tool: str | None
    policy_version: str | None


class RunView(BaseModel):
    """Run inspection: authoritative rows only (never checkpoints), masked payloads only."""

    id: uuid.UUID
    agent_id: str
    status: str
    outcome_summary: str | None
    deadline_at: datetime
    created_at: datetime
    cancel_requested_at: datetime | None
    cancel_requested_by: str | None
    deadline_exceeded_at: datetime | None
    approvals: list[ApprovalView]
    ledger: list[LedgerView]
    audit: list[AuditView]
    audit_chain_valid: bool


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def submit_run(
    request: Request,
    submitted: Annotated[Submission, Depends(submission)],
    task_input: Annotated[dict[str, Any], Body()],
) -> SubmittedRun:
    """TX1: run row, genesis audit event and first ``advance_run`` job, all or nothing.

    The workspace and the agent come from the credentials (API key binding), the requester from
    the validated token; nothing in ``task_input`` identifies anyone.
    """
    settings = request.app.state.settings
    principal = submitted.principal
    scope = WorkspaceScope(WorkspaceId(principal.workspace_id))
    run_id = uuid.uuid4()
    deadline_at = func.clock_timestamp() + func.make_interval(
        0, 0, 0, 0, 0, 0, settings.run_max_duration_seconds
    )
    try:
        async with UnitOfWork(request.app.state.db_engine) as uow:
            await uow.repository(RunsRepository, scope).add(
                {
                    "id": run_id,
                    "agent_id": submitted.agent_id,
                    "requester_snapshot": principal.requester,
                    "task_input": task_input,
                    "status": RunStatus.QUEUED,
                    "deadline_at": deadline_at,
                    # Pin the call-binding key for every call hash of this run (ADR-0010).
                    "binding_key_id": request.app.state.keyring.active_key_id(KeyFamily.BINDING),
                }
            )
            await uow.repository(AuditRepository, scope).append(
                request.app.state.keyring,
                run_id=run_id,
                dedupe_key="run_submitted",
                event_type="RUN_SUBMITTED",
                actor=principal.requester["sub"],
                payload={"agent_id": submitted.agent_id, "requester": principal.requester},
            )
            jobs = uow.repository(JobsRepository, scope)
            await jobs.enqueue(JobKind.ADVANCE_RUN, run_id)
            # TX1's timer (§11.4). The same expression, evaluated after the run insert, makes it
            # due no earlier than the stored deadline it enforces.
            await jobs.enqueue(JobKind.RUN_DEADLINE, run_id, run_after=deadline_at)
    except IntegrityError as error:
        # The agent does not exist in this workspace: not found, as for any out-of-scope id.
        if isinstance(error.orig, pg_errors.ForeignKeyViolation):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        raise
    return SubmittedRun(run_id=run_id, status=RunStatus.QUEUED)


class CancelRequested(BaseModel):
    run_id: uuid.UUID
    status: str
    cancel_requested_at: datetime | None
    cancel_requested_by: str | None


def is_requester(principal: Principal) -> Callable[[RowMapping], bool]:
    return lambda run: bool(run["requester_snapshot"].get("sub") == principal.subject)


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    request: Request,
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(tasks_principal)],
) -> CancelRequested:
    """TX9 for the run's requester; the run driver ends the run (ADR-0006).

    Anyone else, another workspace or an unknown run gets ``404``. Repeating it, or cancelling a
    terminal run, changes nothing.
    """
    return await cancel_as(request, principal, run_id, may_cancel=is_requester(principal))


async def cancel_as(
    request: Request,
    principal: Principal,
    run_id: uuid.UUID,
    *,
    may_cancel: Callable[[RowMapping], bool],
) -> CancelRequested:
    """TX9 in the principal's workspace, if ``may_cancel`` allows it for this run; else ``404``."""
    scope = WorkspaceScope(WorkspaceId(principal.workspace_id))
    async with UnitOfWork(request.app.state.db_engine) as uow:
        runs = uow.repository(RunsRepository, scope)
        run = await runs.lock(run_id)
        if run is None or not may_cancel(run):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        await stop_requests.request_cancel(
            uow, request.app.state.keyring, scope, run, by=principal.subject
        )
        current = await runs.get(run_id)
    current = _required_row(current)
    return CancelRequested(
        run_id=run_id,
        status=current["status"],
        cancel_requested_at=current["cancel_requested_at"],
        cancel_requested_by=current["cancel_requested_by"],
    )


def _required_row(row: Any) -> Any:
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return row


@router.get("/runs/{run_id}")
async def get_run(
    request: Request,
    run_id: uuid.UUID,
    reader: Annotated[RunReader, Depends(run_reader)],
) -> RunView:
    scope = reader.scope
    async with UnitOfWork(request.app.state.db_engine) as uow:
        row = await uow.repository(RunsRepository, scope).get(run_id)
        # With real credentials, only the run's requester may read it (PRD authorization table).
        owner = None if row is None else row["requester_snapshot"].get("sub")
        if row is None or (reader.requester is not None and owner != reader.requester):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        approvals = await uow.repository(ApprovalsRepository, scope).for_run(run_id)
        ledger = await uow.repository(LedgerRepository, scope).for_run(run_id)
        audit_repo = uow.repository(AuditRepository, scope)
        events = await audit_repo.events(run_id)
        chain_valid = await verify_run(audit_repo, request.app.state.keyring, run_id)
    return RunView(
        **{name: row[name] for name in RunView.model_fields if name in row},
        approvals=[ApprovalView.model_validate(dict(a)) for a in approvals],
        ledger=[
            LedgerView(**dict(entry), outcome=entry["outcome_payload_masked"]) for entry in ledger
        ],
        audit=[
            AuditView(
                seq=event.seq,
                event_type=event.event_type,
                actor=event.actor,
                tool=event.tool,
                policy_version=event.policy_version,
            )
            for event in events
        ],
        audit_chain_valid=chain_valid,
    )
