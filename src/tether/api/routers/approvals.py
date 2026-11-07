"""``/v1/approvals``: approve or reject a pending call (TX4).

Walking-skeleton scope: the approver identity comes from the temporary dev/test shortcut
(S1.8 replaces it). Eligibility is decided by policy under the active version (ADR-0009):
the approver needs an approver role from the matching rule and must not be the requester.
Anything the caller may not act on is ``404`` (ADR-0013).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel

from tether.api.dev_identity import Approver, dev_approver, refuse_in_prod
from tether.core.ids import WorkspaceId
from tether.core.statuses import TERMINAL_RUN_STATUSES, JobKind
from tether.policy.engine import evaluate_action, evaluate_approval_eligibility
from tether.storage.repos.approvals import ApprovalsRepository
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.runs import JobsRepository, PolicyVersionsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

router = APIRouter(prefix="/v1/approvals", dependencies=[Depends(refuse_in_prod)])

_DECISIONS = {"approve": "approved", "reject": "rejected"}


class DecidedApproval(BaseModel):
    id: uuid.UUID
    status: str


@router.post("/{approval_id}/{action}")
async def decide_approval(
    request: Request,
    approval_id: uuid.UUID,
    action: Literal["approve", "reject"],
    approver: Annotated[Approver, Depends(dev_approver)],
    reason: Annotated[str, Body(max_length=500)] = "",
) -> DecidedApproval:
    not_found = HTTPException(status.HTTP_404_NOT_FOUND)
    conflict = HTTPException(status.HTTP_409_CONFLICT, "approval is no longer pending")
    scope = WorkspaceScope(WorkspaceId(approver.workspace_id))
    workspace = request.app.state.bootstrap.workspace(approver.workspace_id)
    if workspace is None:
        raise not_found
    new_status = _DECISIONS[action]
    async with UnitOfWork(request.app.state.db_engine) as uow:
        approvals = uow.repository(ApprovalsRepository, scope)
        found = await approvals.get(approval_id)
        if found is None:
            raise not_found
        # Lock order: run row, then approval row.
        run = await uow.repository(RunsRepository, scope).share_lock(found["run_id"])
        approval = await approvals.lock(approval_id)
        if approval is None:
            raise not_found
        active = await uow.repository(PolicyVersionsRepository, scope).active_version()
        if active != workspace.policy_version:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "policy is reloading")
        decision = evaluate_action(
            workspace, tool=approval["tool"], requester=run["requester_snapshot"]
        )
        if not evaluate_approval_eligibility(
            decision, approver=approver.person, requester=run["requester_snapshot"]
        ):
            raise not_found
        if (
            approval["status"] != "pending"
            or not approval["live"]
            or run["stopped"]
            or run["status"] in TERMINAL_RUN_STATUSES
        ):
            raise conflict
        decided = await approvals.decide(
            approval_id,
            status=new_status,
            decided_by=approver.person["sub"],
            reason=reason,
            policy_version=decision.policy_version,
        )
        if not decided:
            raise conflict
        await uow.repository(AuditRepository, scope).append(
            request.app.state.keyring,
            run_id=run["id"],
            dedupe_key=f"approval_decided:{approval_id}",
            event_type="APPROVAL_DECIDED",
            payload={"approval_id": str(approval_id), "status": new_status, "reason": reason},
            actor=approver.person["sub"],
            tool=approval["tool"],
            policy_version=decision.policy_version,
        )
        await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, run["id"])
    return DecidedApproval(id=approval_id, status=new_status)
