"""``/v1/approvals`` (audience ``tether-approvals``): approve or reject a pending call (TX4).

Authorization order (ADR-0013): audience and token, then workspace, then the ``approver`` role
from the validated token, then policy eligibility under the active version (ADR-0009): a role
listed by the matching rule, and never the run's requester. Anything the caller may not act on is
``404``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel

from tether.authz.deps import approvals_principal
from tether.core.ids import WorkspaceId
from tether.core.statuses import TERMINAL_RUN_STATUSES, JobKind
from tether.identity.principal import Principal
from tether.policy.engine import evaluate_action, evaluate_approval_eligibility
from tether.storage.repos.approvals import ApprovalsRepository
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.runs import JobsRepository, PolicyVersionsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

router = APIRouter(prefix="/v1/approvals")

_DECISIONS = {"approve": "approved", "reject": "rejected"}
APPROVER_ROLE = "approver"


class DecidedApproval(BaseModel):
    id: uuid.UUID
    status: str


@router.post("/{approval_id}/{action}")
async def decide_approval(
    request: Request,
    approval_id: uuid.UUID,
    action: Literal["approve", "reject"],
    principal: Annotated[Principal, Depends(approvals_principal)],
    reason: Annotated[str, Body(max_length=500)] = "",
) -> DecidedApproval:
    not_found = HTTPException(status.HTTP_404_NOT_FOUND)
    conflict = HTTPException(status.HTTP_409_CONFLICT, "approval is no longer pending")
    scope = WorkspaceScope(WorkspaceId(principal.workspace_id))
    workspace = request.app.state.bootstrap.workspace(principal.workspace_id)
    if workspace is None or not principal.has_role(APPROVER_ROLE):
        raise not_found
    approver = principal.requester  # ``{sub, roles}`` of the deciding person
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
            decision, approver=approver, requester=run["requester_snapshot"]
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
            decided_by=principal.subject,
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
            actor=principal.subject,
            tool=approval["tool"],
            policy_version=decision.policy_version,
        )
        await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, run["id"])
    return DecidedApproval(id=approval_id, status=new_status)
