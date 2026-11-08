"""``/v1/tasks``: submit a run (TX1) and read it back.

Walking-skeleton scope: identity comes from the temporary dev/test shortcut (S1.8 replaces it),
and TX1 enqueues no ``run_deadline`` job yet (S5.1).
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

from tether.api.dev_identity import Principal, dev_read_scope, dev_submit_principal, refuse_in_prod
from tether.audit.chain import verify_run
from tether.core.ids import WorkspaceId
from tether.core.statuses import JobKind, RunStatus
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
            await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, run_id)
    except IntegrityError as error:
        # The agent does not exist in this workspace: not found, as for any out-of-scope id.
        if isinstance(error.orig, pg_errors.ForeignKeyViolation):
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        raise
    return SubmittedRun(run_id=run_id, status=RunStatus.QUEUED)


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
