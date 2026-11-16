"""``/v1/approvals`` (audience ``tether-approvals``): approve or reject a pending call (TX4).

Authorization order (ADR-0013): audience and token, then workspace, then the ``approver`` role
from the validated token, then policy eligibility under the active version (ADR-0009): a role
listed by the matching rule, and never the run's requester. Anything the caller may not act on is
``404``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.engine import RowMapping

from tether.api.routers.tasks import AuditView, LedgerView
from tether.audit.chain import verify_run
from tether.authz.deps import approvals_principal, approvals_reader
from tether.core.ids import WorkspaceId
from tether.core.statuses import TERMINAL_RUN_STATUSES, JobKind
from tether.identity.principal import Principal
from tether.observability import metrics, tracing
from tether.observability.logging import get_logger
from tether.policy.engine import (
    ApprovalEligibilityRequest,
    BuiltinPolicyEngine,
    TrustedRequester,
)
from tether.storage.repos.approvals import ApprovalsRepository
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import JobsRepository, PolicyVersionsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

router = APIRouter(prefix="/v1/approvals")
_log = get_logger(__name__)

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
    approver = TrustedRequester.from_snapshot(principal.requester)  # the deciding person
    policy = BuiltinPolicyEngine(workspace)
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
        if active != policy.policy_version:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "policy is reloading")
        # Eligibility (ADR-0020): a role named by the tool's require_approval rule, and never
        # the run's requester.
        eligibility = policy.evaluate_approval_eligibility(
            ApprovalEligibilityRequest(
                workspace_id=scope.workspace_id,
                approver=approver,
                requester=TrustedRequester.from_snapshot(run["requester_snapshot"]),
                tool_name=approval["tool"],
            )
        )
        if not eligibility.eligible:
            raise not_found
        if (
            approval["status"] != "pending"
            or not approval["live"]
            or run["stopped"]
            or run["status"] in TERMINAL_RUN_STATUSES
        ):
            raise conflict
        tracing.annotate_current(
            **{"tether.run_id": run["id"], "tether.workspace_id": scope.workspace_id}
        )
        # The decision joins the run's trace, linked to this request (AD-13); its audit event and
        # wake job carry that context.
        with tracing.span(
            "approval.decide",
            parent=run["trace_parent"],
            links=[tracing.current_traceparent()],
            attributes={"tether.tool": approval["tool"], "tether.approval.status": new_status},
        ):
            decided = await approvals.decide(
                approval_id,
                status=new_status,
                decided_by=principal.subject,
                reason=reason,
                policy_version=eligibility.policy_version,
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
                policy_version=eligibility.policy_version,
            )
            await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, run["id"])
        waited = (datetime.now(UTC) - approval["created_at"]).total_seconds()
        uow.on_commit(lambda: metrics.approval_resolved(new_status, waited))
    _log.info(
        "approval decided",
        run_id=run["id"],
        workspace_id=scope.workspace_id,
        tool=approval["tool"],
        status=new_status,
    )
    return DecidedApproval(id=approval_id, status=new_status)


# --- read side (W4.1, T-28): list, detail and run evidence -----------------------------------
#
# Authorization order as for the decision: a valid ``tether-approvals`` token, the core
# ``approver`` role, then policy eligibility under the active version (ADR-0009) and never the
# run's requester. Everything else is ``404``; a stale policy is ``503`` with no data. These routes
# write nothing and return only ``*_masked`` data (ADR-0017). The list is advisory: the decision
# route's own checks remain the authority when the approver acts (U-010).

DEFAULT_PAGE, MAX_PAGE = 50, 200


class ApprovalDetail(BaseModel):
    """Enough masked, trusted context to decide (OD-7). Model rationale is not exposed."""

    id: uuid.UUID
    run_id: uuid.UUID
    status: str
    actionable: bool
    tool: str
    tool_version: str
    arguments_masked: dict[str, Any]
    preconditions_masked: dict[str, Any]
    call_hash: str
    requester: str | None
    agent_id: str
    created_at: datetime
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    policy_version: str | None


class ApprovalPage(BaseModel):
    items: list[ApprovalDetail]
    next_after: uuid.UUID | None


class ApproverRunView(BaseModel):
    """Run evidence for an approver: authoritative rows, masked payloads, no task input."""

    id: uuid.UUID
    status: str
    outcome_summary: str | None
    requester: str | None
    agent_id: str
    created_at: datetime
    deadline_at: datetime
    cancel_requested_at: datetime | None
    cancel_requested_by: str | None
    deadline_exceeded_at: datetime | None
    approvals: list[ApprovalDetail]
    ledger: list[LedgerView]
    audit: list[AuditView]
    audit_chain_valid: bool


@dataclass(frozen=True)
class _Reader:
    scope: WorkspaceScope
    policy: BuiltinPolicyEngine
    approver: TrustedRequester

    def eligible(self, tool: str, requester_snapshot: Any) -> bool:
        return self.policy.evaluate_approval_eligibility(
            ApprovalEligibilityRequest(
                workspace_id=self.scope.workspace_id,
                approver=self.approver,
                requester=TrustedRequester.from_snapshot(requester_snapshot),
                tool_name=tool,
            )
        ).eligible


def _reader(request: Request, principal: Principal) -> _Reader:
    workspace = request.app.state.bootstrap.workspace(principal.workspace_id)
    if workspace is None or not principal.has_role(APPROVER_ROLE):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _Reader(
        scope=WorkspaceScope(WorkspaceId(principal.workspace_id)),
        policy=BuiltinPolicyEngine(workspace),
        approver=TrustedRequester.from_snapshot(principal.requester),
    )


async def _require_current_policy(uow: UnitOfWork, reader: _Reader) -> None:
    active = await uow.repository(PolicyVersionsRepository, reader.scope).active_version()
    if active != reader.policy.policy_version:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "policy is reloading")


def _requester_subject(snapshot: Any) -> str | None:
    subject = snapshot.get("sub")
    return subject if isinstance(subject, str) else None


def _detail(row: RowMapping) -> ApprovalDetail:
    return ApprovalDetail(
        **{name: row[name] for name in ApprovalDetail.model_fields if name in row},
        actionable=bool(row["status"] == "pending" and row["live"] and row["run_active"]),
        requester=_requester_subject(row["requester_snapshot"]),
    )


@router.get("/approvals")
async def list_approvals(
    request: Request,
    principal: Annotated[Principal, Depends(approvals_reader)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    after: uuid.UUID | None = None,
) -> ApprovalPage:
    """Pending approvals the caller could decide right now, oldest first (keyset by ``after``)."""
    reader = _reader(request, principal)
    items: list[ApprovalDetail] = []
    async with UnitOfWork(request.app.state.db_engine) as uow:
        await _require_current_policy(uow, reader)
        approvals = uow.repository(ApprovalsRepository, reader.scope)
        cursor = None
        if after is not None:
            anchor = await approvals.get(after)
            if anchor is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND)
            cursor = (anchor["created_at"], anchor["id"])
        while len(items) <= limit:
            batch = await approvals.pending_after(cursor, MAX_PAGE)
            for row in batch:
                if reader.eligible(row["tool"], row["requester_snapshot"]):
                    items.append(_detail(row))
            if len(batch) < MAX_PAGE:
                break
            cursor = (batch[-1]["created_at"], batch[-1]["id"])
    more = len(items) > limit
    return ApprovalPage(items=items[:limit], next_after=items[limit - 1].id if more else None)


@router.get("/approvals/{approval_id}")
async def get_approval(
    request: Request,
    approval_id: uuid.UUID,
    principal: Annotated[Principal, Depends(approvals_reader)],
) -> ApprovalDetail:
    """One approval, any status, if the caller is eligible for it."""
    reader = _reader(request, principal)
    async with UnitOfWork(request.app.state.db_engine) as uow:
        await _require_current_policy(uow, reader)
        row = await uow.repository(ApprovalsRepository, reader.scope).detail(approval_id)
        if row is None or not reader.eligible(row["tool"], row["requester_snapshot"]):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _detail(row)


@router.get("/runs/{run_id}")
async def get_run_evidence(
    request: Request,
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(approvals_reader)],
) -> ApproverRunView:
    """Evidence for a run with at least one approval the caller is eligible for."""
    reader = _reader(request, principal)
    async with UnitOfWork(request.app.state.db_engine) as uow:
        await _require_current_policy(uow, reader)
        run = await uow.repository(RunsRepository, reader.scope).get(run_id)
        approvals = uow.repository(ApprovalsRepository, reader.scope)
        rows = [await approvals.detail(a["id"]) for a in await approvals.for_run(run_id)]
        details = [_detail(row) for row in rows if row is not None]
        if run is None or not any(
            reader.eligible(row["tool"], row["requester_snapshot"])
            for row in rows
            if row is not None
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        ledger = await uow.repository(LedgerRepository, reader.scope).for_run(run_id)
        audit_repo = uow.repository(AuditRepository, reader.scope)
        events = await audit_repo.events(run_id)
        chain_valid = await verify_run(audit_repo, request.app.state.keyring, run_id)
    return ApproverRunView(
        **{name: run[name] for name in ApproverRunView.model_fields if name in run},
        requester=_requester_subject(run["requester_snapshot"]),
        approvals=details,
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
