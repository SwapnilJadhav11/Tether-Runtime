"""``execute_tool``: the only place a tool's side effect happens (§12, ADR-0007, ADR-0008).

TX6 commits ``pending → dispatched`` (and its audit event) before the external call. A redelivery
of a ``dispatched`` entry re-sends only for idempotent tools after committing TX6r; otherwise the
outcome is unknown and the entry becomes ``indeterminate``. TX7 records the masked outcome,
audits it and wakes the run. Outcomes are honest: an unknown write result is never a failure.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.engine import RowMapping

from tether.core.hashing import call_hashes_equal
from tether.core.statuses import JobKind
from tether.queue.postgres import LeasedJob
from tether.registry.masking import mask
from tether.runtime.driver import WorkerRuntime
from tether.runtime.graph import call_hash_for
from tether.sdk import ToolContext, ToolDescriptor, ToolNotApplied
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import JobsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

TERMINAL_LEDGER_STATES = frozenset({"completed", "failed", "indeterminate", "abandoned"})

Send = tuple[RowMapping, ToolDescriptor, BaseModel]


def _descriptor(
    runtime: WorkerRuntime, scope: WorkspaceScope, run: RowMapping, entry: RowMapping
) -> ToolDescriptor | None:
    workspace = runtime.bootstrap.workspace(scope.workspace_id)
    agent = workspace.agent(run["agent_id"]) if workspace else None
    if agent is None:
        return None
    descriptor = runtime.registries[agent.workload].resolve(entry["tool"])
    if descriptor is None or descriptor.version != entry["tool_version"]:
        return None  # the pinned version is not resolvable (ADR-0011)
    return descriptor


async def _record(
    uow: UnitOfWork,
    runtime: WorkerRuntime,
    scope: WorkspaceScope,
    entry: RowMapping,
    *,
    expected: str,
    new: str,
    output: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    """TX7 (and the not-dispatched failures): outcome + audit + wake the run, atomically."""
    changed = await uow.repository(LedgerRepository, scope).transition(
        entry["id"],
        expected=expected,
        new=new,
        outcome_payload_masked=output,
        outcome_reason=reason,
    )
    if not changed:
        return
    await uow.repository(AuditRepository, scope).append(
        runtime.keys,
        run_id=entry["run_id"],
        dedupe_key=f"outcome:{entry['id']}",
        event_type="TOOL_OUTCOME",
        payload={"ledger_id": str(entry["id"]), "state": new, "reason": reason},
        tool=entry["tool"],
    )
    await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, entry["run_id"])


async def _prepare(runtime: WorkerRuntime, scope: WorkspaceScope, job: LeasedJob) -> Send | None:
    """TX6 / TX6r, committed before any external call. ``None`` means: do not send."""
    if job.ref_id is None:
        return None
    async with UnitOfWork(runtime.engine) as uow:
        ledger = uow.repository(LedgerRepository, scope)
        entry = await ledger.lock(job.ref_id)
        if entry is None or entry["state"] in TERMINAL_LEDGER_STATES:
            return None
        run = await uow.repository(RunsRepository, scope).share_lock(entry["run_id"])
        state = entry["state"]
        descriptor = _descriptor(runtime, scope, run, entry)
        if descriptor is None:
            reason = "not applied: tool version not resolvable"
            if state == "pending":
                await _record(
                    uow, runtime, scope, entry, expected=state, new="failed", reason=reason
                )
            return None
        args = descriptor.input_model.model_validate(job.payload.get("arguments", {}))
        hashed = call_hash_for(runtime.keys, run, descriptor, args)
        if not call_hashes_equal(hashed, entry["call_hash"]):
            reason = "call hash mismatch: not dispatched"
            if state == "pending":
                await _record(
                    uow, runtime, scope, entry, expected=state, new="failed", reason=reason
                )
            return None
        attempt = entry["attempts"] + 1
        if state == "pending":
            if run["stopped"]:
                reason = "not applied: run stopped before dispatch"
                await _record(
                    uow, runtime, scope, entry, expected=state, new="failed", reason=reason
                )
                return None
            event = "DISPATCHED"
        else:
            # Redelivery of a dispatched entry (ADR-0008): re-send only when safe.
            if run["stopped"] or entry["idempotency_support"] != "idempotent":
                unknown = "indeterminate" if entry["effect"] == "write" else "failed"
                reason = "outcome unknown after redelivery; not re-sent"
                await _record(
                    uow, runtime, scope, entry, expected=state, new=unknown, reason=reason
                )
                return None
            event = "REDISPATCHED"
        await ledger.transition(entry["id"], expected=state, new="dispatched", attempts=attempt)
        await uow.repository(AuditRepository, scope).append(
            runtime.keys,
            run_id=entry["run_id"],
            dedupe_key=f"{event.lower()}:{entry['id']}:{attempt}",
            event_type=event,
            payload={"ledger_id": str(entry["id"]), "attempt": attempt},
            tool=entry["tool"],
        )
    return entry, descriptor, args


async def handle(runtime: WorkerRuntime, job: LeasedJob) -> None:
    scope = WorkspaceScope.for_job(
        job_workspace_id=job.workspace_id, run_workspace_id=job.run_workspace_id
    )
    prepared = await _prepare(runtime, scope, job)
    if prepared is None:
        return
    entry, descriptor, args = prepared
    output: dict[str, Any] | None = None
    reason: str | None = None
    context = ToolContext(idempotency_key=entry["idempotency_key"])
    try:
        result = await descriptor.handler(args, context)
        output = mask(descriptor.output_model.model_validate(result.model_dump()))
        new = "completed"
    except ToolNotApplied as not_applied:
        new, reason = "failed", f"not applied: {not_applied.reason}"
    except Exception as error:
        # ADR-0007: a write whose result is unknown is indeterminate, never failed.
        new = "indeterminate" if descriptor.effect == "write" else "failed"
        reason = f"outcome unknown: {type(error).__name__}"
    async with UnitOfWork(runtime.engine) as uow:
        await _record(
            uow, runtime, scope, entry, expected="dispatched", new=new, output=output, reason=reason
        )
