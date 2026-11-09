"""``/v1/ops`` (audience ``tether-ops``): reconcile an ``indeterminate`` entry (TX8, ADR-0007)
and cancel any run of the workspace as its ``workspace_admin`` (TX9).

Authorization order (ADR-0013): audience and token, then workspace, then the role from the
validated token (``reconciler`` or ``workspace_admin``), then, for reconciliation, policy
eligibility under the active version (ADR-0009): a configured reconciler role, and never the
run's requester. Anything the caller may not act on is ``404``.

TX8 commits, in one transaction: the conditional ``indeterminate → completed / failed /
abandoned`` with its resolution, reconciler and justification; the critical ``RECONCILED``
audit event; and the ``advance_run`` wake. The tool is never called again from here.
Repeating the same outcome is a no-op; a different outcome after resolution is ``409``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel

from tether.api.routers.tasks import CancelRequested, cancel_as
from tether.authz.deps import ops_principal
from tether.core.ids import WorkspaceId
from tether.core.statuses import (
    TERMINAL_RUN_STATUSES,
    JobKind,
    LedgerResolution,
    LedgerState,
)
from tether.identity.principal import Principal
from tether.observability import metrics, tracing
from tether.observability.logging import get_logger
from tether.policy.engine import evaluate_reconciliation_eligibility
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import JobsRepository, PolicyVersionsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

router = APIRouter(prefix="/v1/ops")
_log = get_logger(__name__)

RECONCILER_ROLE = "reconciler"
WORKSPACE_ADMIN_ROLE = "workspace_admin"

Outcome = Literal["applied", "not_applied", "abandon"]

_RESOLUTIONS: dict[str, tuple[LedgerState, LedgerResolution]] = {
    "applied": (LedgerState.COMPLETED, LedgerResolution.RECONCILED_APPLIED),
    "not_applied": (LedgerState.FAILED, LedgerResolution.RECONCILED_NOT_APPLIED),
    "abandon": (LedgerState.ABANDONED, LedgerResolution.RECONCILED_ABANDON),
}


class Reconciled(BaseModel):
    ledger_id: uuid.UUID
    state: str
    resolution: str


@router.post("/ledger/{ledger_id}/reconcile")
async def reconcile_entry(
    request: Request,
    ledger_id: uuid.UUID,
    principal: Annotated[Principal, Depends(ops_principal)],
    outcome: Annotated[Outcome, Body()],
    justification: Annotated[str, Body(min_length=1, max_length=2000)],
) -> Reconciled:
    not_found = HTTPException(status.HTTP_404_NOT_FOUND)
    conflict = HTTPException(
        status.HTTP_409_CONFLICT, "ledger entry is not awaiting reconciliation"
    )
    scope = WorkspaceScope(WorkspaceId(principal.workspace_id))
    workspace = request.app.state.bootstrap.workspace(principal.workspace_id)
    if workspace is None or not principal.has_role(RECONCILER_ROLE):
        raise not_found
    reconciler = principal.requester  # ``{sub, roles}`` of the reconciling person
    new_state, resolution = _RESOLUTIONS[outcome]
    resolved = Reconciled(ledger_id=ledger_id, state=new_state, resolution=resolution)
    async with UnitOfWork(request.app.state.db_engine) as uow:
        ledger = uow.repository(LedgerRepository, scope)
        found = await ledger.get(ledger_id)
        if found is None:
            raise not_found
        # Lock order: run row, then ledger entry (as TX4).
        run = await uow.repository(RunsRepository, scope).share_lock(found["run_id"])
        entry = await ledger.lock(ledger_id)
        if entry is None:
            raise not_found
        active = await uow.repository(PolicyVersionsRepository, scope).active_version()
        if active != workspace.policy_version:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "policy is reloading")
        if not evaluate_reconciliation_eligibility(
            workspace, reconciler=reconciler, requester=run["requester_snapshot"]
        ):
            raise not_found
        if entry["state"] != LedgerState.INDETERMINATE:
            if entry["resolution"] == resolution:
                return resolved  # the same reconciliation again: nothing to do
            raise conflict
        if run["status"] in TERMINAL_RUN_STATUSES:
            raise conflict
        tracing.annotate_current(
            **{"tether.run_id": run["id"], "tether.workspace_id": scope.workspace_id}
        )
        # TX8 joins the run's trace, linked to this request (AD-13).
        with tracing.span(
            "ledger.reconcile",
            parent=run["trace_parent"],
            links=[tracing.current_traceparent()],
            attributes={"tether.tool": entry["tool"], "tether.reconciliation.outcome": outcome},
        ):
            changed = await ledger.transition(
                ledger_id,
                expected=LedgerState.INDETERMINATE,
                new=new_state,
                resolution=resolution,
                resolved_by=principal.subject,
                justification=justification,
            )
            if not changed:
                raise conflict
            await uow.repository(AuditRepository, scope).append(
                request.app.state.keyring,
                run_id=run["id"],
                dedupe_key=f"reconciled:{ledger_id}",
                event_type="RECONCILED",
                payload={
                    "ledger_id": str(ledger_id),
                    "outcome": outcome,
                    "resolution": resolution.value,
                    "justification": justification,
                },
                actor=principal.subject,
                tool=entry["tool"],
                policy_version=workspace.policy_version,
            )
            await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, run["id"])
        uow.on_commit(lambda: metrics.reconciled(outcome))
    _log.info(
        "ledger entry reconciled",
        run_id=run["id"],
        workspace_id=scope.workspace_id,
        tool=entry["tool"],
        outcome=outcome,
    )
    return resolved


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def admin_cancel_run(
    request: Request,
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(ops_principal)],
) -> CancelRequested:
    """TX9 for a ``workspace_admin``: any run in their workspace (PRD authorization table).

    Approvers cannot cancel (they reject). Anyone without the role, another workspace or an
    unknown run gets ``404``.
    """
    if not principal.has_role(WORKSPACE_ADMIN_ROLE):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return await cancel_as(request, principal, run_id, may_cancel=lambda run: True)
