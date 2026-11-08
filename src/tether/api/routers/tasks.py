"""``/v1/tasks``: submit a run (TX1), read it back, and cancel it (TX9).

Identity comes from the temporary dev/test shortcut (S1.8 replaces it).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from psycopg import errors as pg_errors
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from tether.api.dev_identity import (
    Canceller,
    Principal,
    dev_canceller,
    dev_read_scope,
    dev_submit_principal,
    refuse_in_prod,
)
from tether.audit.chain import verify_run
from tether.core.ids import WorkspaceId
from tether.core.statuses import JobKind, RunStatus
from tether.runtime import stop_requests
from tether.secrets.keyring import KeyFamily
from tether.storage.repos.approvals import ApprovalsRepository
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import JobsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

router = APIRouter(prefix="/v1/tasks", dependencies=[Depends(refuse_in_prod)])


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
    principal: Annotated[Principal, Depends(dev_submit_principal)],
    task_input: Annotated[dict[str, Any], Body()],
) -> SubmittedRun:
    """TX1: run row, genesis audit event and first ``advance_run`` job, all or nothing."""
    settings = request.app.state.settings
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
                    "agent_id": principal.agent_id,
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
                payload={"agent_id": principal.agent_id, "requester": principal.requester},
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


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    request: Request,
    run_id: uuid.UUID,
    canceller: Annotated[Canceller, Depends(dev_canceller)],
) -> CancelRequested:
    """TX9: record the requester's cancel request; the run driver ends the run (ADR-0006).

    Only the run's requester may cancel it here; anyone else, another workspace or an unknown run
    gets ``404``. Repeating it, or cancelling a terminal run, changes nothing.
    """
    scope = WorkspaceScope(WorkspaceId(canceller.workspace_id))
    async with UnitOfWork(request.app.state.db_engine) as uow:
        runs = uow.repository(RunsRepository, scope)
        run = await runs.lock(run_id)
        if run is None or run["requester_snapshot"].get("sub") != canceller.sub:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        await stop_requests.request_cancel(
            uow, request.app.state.keyring, scope, run, by=canceller.sub
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
    scope: Annotated[WorkspaceScope, Depends(dev_read_scope)],
) -> RunView:
    async with UnitOfWork(request.app.state.db_engine) as uow:
        row = await uow.repository(RunsRepository, scope).get(run_id)
        if row is None:
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
