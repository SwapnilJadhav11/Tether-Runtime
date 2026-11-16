"""``verify_outcome``: bounded automatic verification of an ``indeterminate`` write (ADR-0019 rules
7-12, ADR-0022, architecture §12.2.1).

The job is enqueued by the transaction that moved a write whose pinned version declares ``verify``
to ``indeterminate`` (TX7, TX6r or TX12). Each delivery is one probe:

- **TX-V1, the gate**, committed before the probe. Lock order: run row → ledger row → active
  policy version → audit head → jobs (§13.1). The entry must still be ``indeterminate`` and
  unresolved and verification must not have ended. Then, in order: the stop predicate must be
  false; the run must have no recorded replay divergence; the run's agent and the pinned version
  must resolve, with a declaration; attempts must be below the declaration's bound; the stored
  arguments must validate; this process's policy must be the active version; and
  ``evaluate_verification`` must ``allow`` (ADR-0022). Any refusal ends verification with
  ``VERIFICATION_EXHAUSTED`` and its reason (plus ``VERIFICATION_SKIPPED`` for a stop) and a wake;
  a person reconciles (TX8). Otherwise ``verification_attempts + 1`` and
  ``VERIFICATION_ATTEMPTED``.
- **The probe**: the declared read-back only, never the handler, with the hash-checked original
  arguments and the idempotency key, bounded by the tool's ``timeout_seconds``.
- **TX-V2**: a definitive answer resolves the entry conditionally on it still being
  ``indeterminate`` and unresolved (``completed``/``failed``, ``verified_*``,
  ``system:verification``, masked evidence) with the critical ``VERIFIED`` event and a wake. An
  inconclusive answer records ``VERIFICATION_INCONCLUSIVE`` and, while attempts remain and no stop
  is active, the job is released for the next probe after the declared backoff; otherwise
  verification ends as above. If a person (or another probe) resolved the entry first, the answer
  is ``VERIFICATION_LATE`` evidence only and changes nothing: the human wins.

These are worker transactions of the TX6/TX7 class (ADR-0005 rule 7): serialised by the ledger
row's lock and conditional updates, not by the driver's fencing token. Nothing here re-sends the
write or consumes its send budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.engine import RowMapping

from tether.core.statuses import JobKind, LedgerResolution, LedgerState
from tether.observability import metrics, tracing
from tether.observability.logging import bound, get_logger
from tether.policy.engine import (
    ActionContext,
    BuiltinPolicyEngine,
    PolicyDecision,
    ToolPolicyMetadata,
    TrustedAgent,
    TrustedRequester,
    VerificationRequest,
)
from tether.queue import backoff
from tether.queue.postgres import LeasedJob
from tether.runtime.driver import WorkerRuntime
from tether.runtime.graph import call_hash_for
from tether.runtime.verification import VERIFIED_BY, ProbeAnswer, run_probe
from tether.sdk import ToolDescriptor, VerifyContext, VerifyDeclaration
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import JobsRepository, PolicyVersionsRepository, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

_log = get_logger(__name__)

_RESOLVED_AS = {
    "confirmed_applied": (LedgerState.COMPLETED, LedgerResolution.VERIFIED_APPLIED),
    "confirmed_not_applied": (LedgerState.FAILED, LedgerResolution.VERIFIED_NOT_APPLIED),
}


@dataclass(frozen=True)
class _Probe:
    entry: RowMapping
    descriptor: ToolDescriptor
    declaration: VerifyDeclaration
    arguments: BaseModel
    attempt: int


class _StaleLease(Exception):  # noqa: N818 - a control-flow signal that rolls TX12 back
    """Another lessee owns the job now: nothing may commit."""


def _scope(job: LeasedJob) -> WorkspaceScope:
    return WorkspaceScope.for_job(
        job_workspace_id=job.workspace_id, run_workspace_id=job.run_workspace_id
    )


def verification_payload(descriptor: ToolDescriptor, arguments: Any) -> dict[str, Any] | None:
    """The ``verify_outcome`` job payload for an entry going ``indeterminate``, or ``None`` when its
    pinned version declares no ``verify`` (ADR-0022 rule 10: then nothing is verified)."""
    if descriptor.verify is None or descriptor.effect != "write":
        return None
    return {"arguments": arguments}


async def _end(
    uow: UnitOfWork,
    runtime: WorkerRuntime,
    scope: WorkspaceScope,
    entry: RowMapping,
    reason: str,
    *,
    policy: PolicyDecision | None = None,
) -> None:
    """End automatic verification: the entry stays ``indeterminate`` for a person (rule 11)."""
    audit = uow.repository(AuditRepository, scope)
    ordinal = entry["step_ordinal"]
    if reason == "stopped":
        # Stop precedence (ADR-0019 rule 9): no probe starts.
        await audit.append(
            runtime.keys,
            run_id=entry["run_id"],
            dedupe_key=f"verification_skipped:{ordinal}",
            event_type="VERIFICATION_SKIPPED",
            payload={"ledger_id": str(entry["id"]), "reason": reason},
            tool=entry["tool"],
        )
    payload: dict[str, Any] = {
        "ledger_id": str(entry["id"]),
        "reason": reason,
        "attempts": entry["verification_attempts"],
    }
    if policy is not None:
        payload["policy_reasons"] = list(policy.reasons)
    await audit.append(
        runtime.keys,
        run_id=entry["run_id"],
        dedupe_key=f"verification_exhausted:{ordinal}",
        event_type="VERIFICATION_EXHAUSTED",
        payload=payload,
        tool=entry["tool"],
        policy_version=None if policy is None else policy.policy_version,
    )
    await uow.repository(JobsRepository, scope).enqueue(JobKind.ADVANCE_RUN, entry["run_id"])
    tool = str(entry["tool"])

    def ended() -> None:
        metrics.verification(tool, "exhausted")
        _log.warning("verification ended; reconciliation required", tool=tool, reason=reason)

    uow.on_commit(ended)


async def _ended(audit: AuditRepository, entry: RowMapping) -> bool:
    found = await audit.find(entry["run_id"], f"verification_exhausted:{entry['step_ordinal']}")
    return found is not None


def _unresolved(entry: RowMapping | None) -> bool:
    return (
        entry is not None
        and entry["state"] == LedgerState.INDETERMINATE
        and entry["resolution"] is None
    )


async def _gate(runtime: WorkerRuntime, scope: WorkspaceScope, job: LeasedJob) -> _Probe | None:
    """TX-V1, committed before the probe. ``None``: no probe (nothing to verify, or it ended)."""
    if job.ref_id is None:
        return None
    async with UnitOfWork(runtime.engine) as uow:
        run = await uow.repository(RunsRepository, scope).share_lock(job.run_id)
        ledger = uow.repository(LedgerRepository, scope)
        entry = await ledger.lock(job.ref_id)
        audit = uow.repository(AuditRepository, scope)
        if entry is None or not _unresolved(entry) or await _ended(audit, entry):
            return None
        if run["stopped"]:
            await _end(uow, runtime, scope, entry, "stopped")
            return None
        if await audit.has_event(entry["run_id"], "REPLAY_DIVERGENCE"):
            await _end(uow, runtime, scope, entry, "replay_divergence")
            return None
        workspace = runtime.bootstrap.workspace(scope.workspace_id)
        agent = workspace.agent(run["agent_id"]) if workspace else None
        if workspace is None or agent is None:
            await _end(uow, runtime, scope, entry, "configuration_unavailable")
            return None
        # Exactly the pinned version, never another one (ADR-0021 rule 14, ADR-0022 rule 5).
        registry = runtime.registries[agent.workload]
        descriptor = registry.resolve(entry["tool"], entry["tool_version"])
        if descriptor is None:
            await _end(uow, runtime, scope, entry, "tool_version_unavailable")
            return None
        declaration = descriptor.verify
        if declaration is None:
            await _end(uow, runtime, scope, entry, "not_declared")
            return None
        if entry["verification_attempts"] >= declaration.max_attempts:
            await _end(uow, runtime, scope, entry, "attempts_exhausted")
            return None
        try:
            arguments = descriptor.input_model.model_validate(job.payload.get("arguments", {}))
        except ValidationError:
            await _end(uow, runtime, scope, entry, "arguments_invalid")
            return None
        policy = BuiltinPolicyEngine(workspace)
        active = await uow.repository(PolicyVersionsRepository, scope).active_version()
        if active != policy.policy_version:
            await _end(uow, runtime, scope, entry, "policy_stale")
            return None
        authorized = await audit.find(entry["run_id"], f"authorized:{entry['step_ordinal']}")
        decision = policy.evaluate_verification(
            VerificationRequest(
                workspace_id=scope.workspace_id,
                requester=TrustedRequester.from_snapshot(run["requester_snapshot"]),
                agent=TrustedAgent(run["agent_id"]),
                tool=ToolPolicyMetadata.from_descriptor(descriptor),
                pinned_version=entry["tool_version"],
                context=ActionContext(
                    run_id=str(entry["run_id"]),
                    step_ordinal=entry["step_ordinal"],
                    call_hash=entry["call_hash"],
                ),
                probe_call_hash=call_hash_for(runtime.keys, run, descriptor, arguments),
                authorized_policy_version=None if authorized is None else authorized.policy_version,
            )
        )
        if decision.outcome != "allow":
            await _end(uow, runtime, scope, entry, "policy_denied", policy=decision)
            return None
        attempt = entry["verification_attempts"] + 1
        await ledger.transition(
            entry["id"],
            expected=LedgerState.INDETERMINATE,
            new=LedgerState.INDETERMINATE,
            verification_attempts=attempt,
        )
        await audit.append(
            runtime.keys,
            run_id=entry["run_id"],
            dedupe_key=f"verification_attempted:{entry['step_ordinal']}:{attempt}",
            event_type="VERIFICATION_ATTEMPTED",
            payload={
                "ledger_id": str(entry["id"]),
                "attempt": attempt,
                "max_attempts": declaration.max_attempts,
            },
            actor=VERIFIED_BY,
            tool=entry["tool"],
            policy_version=decision.policy_version,
        )
    return _Probe(entry, descriptor, declaration, arguments, attempt)


async def _record(
    runtime: WorkerRuntime, scope: WorkspaceScope, probe: _Probe, answer: ProbeAnswer
) -> float | None:
    """TX-V2, or the inconclusive record. Returns the delay before the next probe, if any."""
    entry_id = probe.entry["id"]
    ordinal = probe.entry["step_ordinal"]
    tool = str(probe.entry["tool"])
    retry_in: float | None = None
    async with UnitOfWork(runtime.engine) as uow:
        # Lock order: run row → ledger row → audit head → jobs (§13.1).
        run = await uow.repository(RunsRepository, scope).share_lock(probe.entry["run_id"])
        ledger = uow.repository(LedgerRepository, scope)
        entry = await ledger.lock(entry_id)
        audit = uow.repository(AuditRepository, scope)
        evidence = {
            "ledger_id": str(entry_id),
            "attempt": probe.attempt,
            "result": answer.kind,
            "reason": answer.reason,
            "evidence": answer.evidence,
        }
        if entry is None or not _unresolved(entry):
            # Resolved first (TX8, or another probe): evidence only; the first resolution stands.
            await audit.append(
                runtime.keys,
                run_id=probe.entry["run_id"],
                dedupe_key=f"verification_late:{ordinal}:{probe.attempt}",
                event_type="VERIFICATION_LATE",
                payload={**evidence, "resolution": None if entry is None else entry["resolution"]},
                actor=VERIFIED_BY,
                tool=tool,
            )
            uow.on_commit(lambda: metrics.verification(tool, "late"))
            return None
        if answer.definitive:
            new_state, resolution = _RESOLVED_AS[answer.kind]
            await ledger.transition(
                entry_id,
                expected=LedgerState.INDETERMINATE,
                new=new_state,
                resolution=resolution,
                resolved_by=VERIFIED_BY,
                verification_evidence_masked=answer.evidence,
            )
            await audit.append(
                runtime.keys,
                run_id=probe.entry["run_id"],
                dedupe_key=f"verified:{ordinal}",
                event_type="VERIFIED",
                payload={**evidence, "resolution": resolution.value},
                actor=VERIFIED_BY,
                tool=tool,
            )
            await uow.repository(JobsRepository, scope).enqueue(
                JobKind.ADVANCE_RUN, probe.entry["run_id"]
            )

            def verified() -> None:
                metrics.verification(tool, answer.kind)
                _log.info("ledger entry verified", tool=tool, resolution=resolution.value)

            uow.on_commit(verified)
            return None
        await audit.append(
            runtime.keys,
            run_id=probe.entry["run_id"],
            dedupe_key=f"verification_inconclusive:{ordinal}:{probe.attempt}",
            event_type="VERIFICATION_INCONCLUSIVE",
            payload=evidence,
            actor=VERIFIED_BY,
            tool=tool,
        )
        uow.on_commit(lambda: metrics.verification(tool, "inconclusive"))
        if run["stopped"]:
            await _end(uow, runtime, scope, entry, "stopped")
        elif entry["verification_attempts"] >= probe.declaration.max_attempts:
            await _end(uow, runtime, scope, entry, "attempts_exhausted")
        else:
            retry_in = backoff.verify_backoff_seconds(probe.declaration.backoff, probe.attempt)
    return retry_in


async def handle(runtime: WorkerRuntime, job: LeasedJob) -> float | None:
    """One probe. Returns the delay before the next probe, or ``None`` when the job is done."""
    scope = _scope(job)
    probe = await _gate(runtime, scope, job)
    if probe is None:
        return None
    descriptor = probe.descriptor
    with (
        bound(tool=descriptor.name),
        tracing.span(
            "tool.verify",
            # Identifiers and bounded values only: never arguments or evidence (ADR-0017).
            attributes={
                "tether.tool": descriptor.name,
                "tether.tool.version": descriptor.version,
                "tether.verification.attempt": probe.attempt,
                "tether.verification.max_attempts": probe.declaration.max_attempts,
            },
        ) as current,
    ):
        started = time.monotonic()
        answer = await run_probe(
            probe.declaration,
            probe.arguments,
            VerifyContext(idempotency_key=probe.entry["idempotency_key"]),
            descriptor.timeout_seconds,
        )
        tracing.annotate(
            current,
            **{
                "tether.verification.result": answer.kind,
                "tether.verification.duration_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
        return await _record(runtime, scope, probe, answer)


async def dead_letter(runtime: WorkerRuntime, job: LeasedJob) -> None:
    """TX12 for ``verify_outcome``: verification ends (``delivery_exhausted``) in the transaction
    that marks the job ``dead``; the entry stays ``indeterminate`` for a person. Refused as a whole
    if this lease is no longer current."""
    scope = _scope(job)
    try:
        async with UnitOfWork(runtime.engine) as uow:
            await uow.repository(RunsRepository, scope).share_lock(job.run_id)
            entry = None
            if job.ref_id is not None:
                entry = await uow.repository(LedgerRepository, scope).lock(job.ref_id)
            audit = uow.repository(AuditRepository, scope)
            if entry is not None and _unresolved(entry) and not await _ended(audit, entry):
                await _end(uow, runtime, scope, entry, "delivery_exhausted")
            if not await uow.repository(JobsRepository, scope).mark_dead(job.id, job.lease_token):
                raise _StaleLease
            uow.on_commit(lambda: metrics.job_dead_lettered(job.kind))
    except _StaleLease:
        return
