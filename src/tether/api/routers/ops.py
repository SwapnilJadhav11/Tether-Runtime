"""``/v1/ops``: resolve an ``indeterminate`` ledger entry (TX8, ADR-0007).

Only a human resolves an unknown write outcome. The reconciler identity comes from the temporary
dev/test shortcut (S1.8 replaces it). Eligibility is policy, decided under the active version
(ADR-0009): a configured reconciler role, and never the run's requester. Anything the caller may
not act on is ``404`` (ADR-0013).

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

from tether.api.dev_identity import Reconciler, dev_reconciler, refuse_in_prod
from tether.core.ids import WorkspaceId
from tether.core.statuses import (
    TERMINAL_RUN_STATUSES,
    JobKind,
    LedgerResolution,
    LedgerState,
)
from tether.policy.engine import evaluate_reconciliation_eligibility
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import JobsRepository, PolicyVersionsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

router = APIRouter(prefix="/v1/ops", dependencies=[Depends(refuse_in_prod)])

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
    reconciler: Annotated[Reconciler, Depends(dev_reconciler)],
    outcome: Annotated[Outcome, Body()],
    justification: Annotated[str, Body(min_length=1, max_length=2000)],
) -> Reconciled:
    not_found = HTTPException(status.HTTP_404_NOT_FOUND)
    conflict = HTTPException(
        status.HTTP_409_CONFLICT, "ledger entry is not awaiting reconciliation"
    )
    scope = WorkspaceScope(WorkspaceId(reconciler.workspace_id))
    workspace = request.app.state.bootstrap.workspace(reconciler.workspace_id)
    if workspace is None:
        raise not_found
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
            workspace, reconciler=reconciler.person, requester=run["requester_snapshot"]
        ):
            raise not_found
        if entry["state"] != LedgerState.INDETERMINATE:
            if entry["resolution"] == resolution:
                return resolved  # the same reconciliation again: nothing to do
            raise conflict
        if run["status"] in TERMINAL_RUN_STATUSES:
            raise conflict
        changed = await ledger.transition(
            ledger_id,
            expected=LedgerState.INDETERMINATE,
            new=new_state,
            resolution=resolution,
            resolved_by=reconciler.person["sub"],
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
            actor=reconciler.person["sub"],
            tool=entry["tool"],
            policy_version=workspace.policy_version,
        )
        await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, run["id"])
    return resolved
