"""``execute_tool``: the only place a tool's side effect happens (§12, ADR-0007, ADR-0008).

TX6 commits ``pending → dispatched`` (and its audit event) before the external call. TX7 records
the masked outcome, audits it and wakes the run. Outcomes are honest: an unknown write result is
never a failure.

Retries (§12.3, §12.4): an unknown outcome of a read or an idempotent write leaves the entry
``dispatched`` and the job is released with a PD-13 backoff as its ``run_after``. The next
delivery re-sends with the same key, but only after committing the stop-gated TX6r record. The
tool's send budget (PD-14) is shared by retries and redeliveries; when it is spent, the unknown
outcome is final: ``indeterminate`` for a write, a typed failure for a read. A non-idempotent
write is never re-sent. ``dead_letter`` is TX12 for a job delivered past ``max_attempts``.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import RowMapping

from tether.core.hashing import call_hashes_equal
from tether.core.statuses import JobKind
from tether.observability import metrics, tracing
from tether.observability.logging import bound, get_logger
from tether.queue import backoff
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
_log = get_logger(__name__)

# entry (as before this send), descriptor, validated arguments, this send's attempt number
Send = tuple[RowMapping, ToolDescriptor, BaseModel, int]


class _StaleLease(Exception):  # noqa: N818 - a control-flow signal that rolls TX12 back
    """Another lessee owns the job now: TX12 must not commit."""


def _resendable(entry: RowMapping) -> bool:
    # Reads are recorded as idempotent: sending one again has no effect to duplicate.
    return bool(entry["idempotency_support"] == "idempotent")


def _unknown_state(entry: RowMapping) -> str:
    """Where an unknown outcome ends once it may not be re-sent (ADR-0007)."""
    return "indeterminate" if entry["effect"] == "write" else "failed"


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
    tool = str(entry["tool"])

    def recorded() -> None:
        if new == "indeterminate":
            metrics.ledger_indeterminate(tool)
            _log.warning("tool outcome indeterminate", tool=tool, reason=reason)
        else:
            _log.info("tool outcome", tool=tool, state=new, reason=reason)

    uow.on_commit(recorded)


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
            # A re-send of a dispatched entry, retry or redelivery (ADR-0008): only when safe.
            refusal: str | None = None
            if run["stopped"] or not _resendable(entry):
                refusal = "outcome unknown after redelivery; not re-sent"
            elif entry["attempts"] >= descriptor.max_attempts:
                refusal = f"outcome unknown after {entry['attempts']} attempts; not re-sent"
            if refusal is not None:
                await _record(
                    uow,
                    runtime,
                    scope,
                    entry,
                    expected=state,
                    new=_unknown_state(entry),
                    reason=refusal,
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
    return entry, descriptor, args, attempt


async def handle(runtime: WorkerRuntime, job: LeasedJob) -> float | None:
    """Run one delivery. Returns the retry delay in seconds, or ``None`` when the job is done."""
    scope = WorkspaceScope.for_job(
        job_workspace_id=job.workspace_id, run_workspace_id=job.run_workspace_id
    )
    prepared = await _prepare(runtime, scope, job)
    if prepared is None:
        return None
    entry, descriptor, args, attempt = prepared
    with bound(tool=descriptor.name):
        return await _send(runtime, scope, entry, descriptor, args, attempt)


async def _send(
    runtime: WorkerRuntime,
    scope: WorkspaceScope,
    entry: RowMapping,
    descriptor: ToolDescriptor,
    args: BaseModel,
    attempt: int,
) -> float | None:
    """One send to the tool and its TX7; returns the retry delay, or ``None`` when done."""
    output: dict[str, Any] | None = None
    reason: str | None = None
    context = ToolContext(idempotency_key=entry["idempotency_key"])
    # Identifiers and bounded descriptors only: never arguments or outputs (ADR-0017).
    with tracing.span(
        "tool.execute",
        attributes={
            "tether.tool": descriptor.name,
            "tether.tool.version": descriptor.version,
            "tether.tool.effect": descriptor.effect,
            "tether.tool.attempt": attempt,
            "tether.tool.max_attempts": descriptor.max_attempts,
        },
    ) as current:
        started = time.monotonic()
        retry_in: float | None = None
        try:
            result = await descriptor.handler(args, context)
            output = mask(descriptor.output_model.model_validate(result.model_dump()))
            new = outcome = "completed"
        except ToolNotApplied as not_applied:
            new, reason, outcome = "failed", f"not applied: {not_applied.reason}", "not_applied"
        except Exception as error:
            outcome = "unknown"
            tracing.mark_error(current, type(error).__name__)
            # Unknown outcome. Re-send with the same key while the budget lasts, but only when
            # that cannot duplicate an effect. Invalid output is never retried (PD-33).
            retry = (
                _resendable(entry)
                and attempt < descriptor.max_attempts
                and not isinstance(error, ValidationError)
            )
            if retry:
                retry_in = backoff.tool_backoff_seconds(attempt)
                metrics.tool_retry(descriptor.name)
                tracing.event("tool.retry_scheduled", **{"tether.retry.delay_s": retry_in})
                _log.warning(
                    "tool outcome unknown; retry scheduled",
                    attempt=attempt,
                    error=type(error).__name__,
                    delay_s=round(retry_in, 3),
                )
            # ADR-0007: a write whose result is unknown is indeterminate, never failed.
            new, reason = _unknown_state(entry), f"outcome unknown: {type(error).__name__}"
        metrics.tool_executed(descriptor.name, outcome, time.monotonic() - started)
        tracing.annotate(current, **{"tether.tool.outcome": outcome})
    if retry_in is not None:
        return retry_in
    async with UnitOfWork(runtime.engine) as uow:
        # TX7. Lock order: run row → ledger row → audit head → jobs (§13.1). Without the run row
        # first, the inserts' foreign-key checks lock it after the audit head, which deadlocks
        # with a stop (TX9/TX10/TX-EA) holding the run row and waiting for that head.
        await uow.repository(RunsRepository, scope).share_lock(entry["run_id"])
        await _record(
            uow, runtime, scope, entry, expected="dispatched", new=new, output=output, reason=reason
        )
    return None


async def dead_letter(runtime: WorkerRuntime, job: LeasedJob) -> None:
    """TX12 for ``execute_tool``: the entry's domain outcome, in the transaction that marks the job
    ``dead`` (ADR-0004). Nothing is sent. Refused as a whole if this lease is no longer current."""
    scope = WorkspaceScope.for_job(
        job_workspace_id=job.workspace_id, run_workspace_id=job.run_workspace_id
    )
    try:
        async with UnitOfWork(runtime.engine) as uow:
            # Lock order: run row → ledger row → audit head → jobs (§13.1).
            await uow.repository(RunsRepository, scope).share_lock(job.run_id)
            entry = None
            if job.ref_id is not None:
                entry = await uow.repository(LedgerRepository, scope).lock(job.ref_id)
            if entry is not None and entry["state"] == "pending":
                # No call is reachable without TX6: this one never left Tether.
                reason = "not applied: delivery exhausted before dispatch"
                await _record(
                    uow, runtime, scope, entry, expected="pending", new="failed", reason=reason
                )
            elif entry is not None and entry["state"] == "dispatched":
                reason = "outcome unknown: delivery exhausted"
                await _record(
                    uow,
                    runtime,
                    scope,
                    entry,
                    expected="dispatched",
                    new=_unknown_state(entry),
                    reason=reason,
                )
            if not await uow.repository(JobsRepository, scope).mark_dead(job.id, job.lease_token):
                raise _StaleLease
            uow.on_commit(lambda: _dead_lettered(job))
    except _StaleLease:
        return


def _dead_lettered(job: LeasedJob) -> None:
    metrics.job_dead_lettered(job.kind)
    _log.warning("job dead-lettered", attempts=job.attempts, max_attempts=job.max_attempts)
