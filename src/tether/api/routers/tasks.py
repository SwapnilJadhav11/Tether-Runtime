"""``/v1/tasks``: submit a run (TX1) and read it back.

Walking-skeleton scope: identity comes from the temporary dev/test shortcut (S1.8 replaces it),
and TX1 writes no ``RUN_SUBMITTED`` audit event and no ``run_deadline`` job yet (S1.6, S5.1).
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
from tether.core.ids import WorkspaceId
from tether.core.statuses import JobKind, RunStatus
from tether.storage.repos.runs import JobsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

# PD-14: advance_run jobs get 8 attempts.
ADVANCE_RUN_MAX_ATTEMPTS = 8

router = APIRouter(prefix="/v1/tasks", dependencies=[Depends(refuse_in_prod)])


class SubmittedRun(BaseModel):
    run_id: uuid.UUID
    status: str


class RunView(BaseModel):
    id: uuid.UUID
    agent_id: str
    status: str
    outcome_summary: str | None
    deadline_at: datetime
    created_at: datetime


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def submit_run(
    request: Request,
    principal: Annotated[Principal, Depends(dev_submit_principal)],
    task_input: Annotated[dict[str, Any], Body()],
) -> SubmittedRun:
    """TX1: the run row and its first ``advance_run`` job commit together or not at all."""
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
                }
            )
            await uow.repository(JobsRepository, scope).add(
                {
                    "id": uuid.uuid4(),
                    "kind": JobKind.ADVANCE_RUN,
                    "run_id": run_id,
                    "max_attempts": ADVANCE_RUN_MAX_ATTEMPTS,
                }
            )
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
    return RunView.model_validate(dict(row))
