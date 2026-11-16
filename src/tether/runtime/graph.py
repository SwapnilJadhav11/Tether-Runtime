"""The governed run graph (§9.2), compiled per invocation over a ``RunContext``.

Write nodes (``govern_call``, ``authorize_call``, ``finalize``) share-lock the run row, assert the
fencing token and check the stop predicate, and write their audit event in the same
transaction. Wait nodes (``await_tool``) only read the authoritative row and interrupt.
Decisions are keyed by step ordinal and adopted on replay, never re-decided.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.core.hashing import CallEnvelope, call_hash, call_hashes_equal, idempotency_key
from tether.core.ids import AgentId, RunId, WorkspaceId
from tether.core.results import CallOutcome
from tether.core.statuses import JobKind, LedgerResolution, OutcomeClass, RunStatus
from tether.observability import metrics, tracing
from tether.observability.logging import get_logger
from tether.policy.config import WorkspaceConfig
from tether.policy.engine import (
    ActionContext,
    ActionRequest,
    PolicyEngine,
    PolicyStaleError,
    ToolPolicyMetadata,
    TrustedAgent,
    TrustedRequester,
)
from tether.providers.history import ModelTurn, NeutralMessage
from tether.providers.protocol import ModelProvider, ProviderError
from tether.queue import backoff
from tether.registry.masking import mask
from tether.registry.registry import ToolRegistry
from tether.sdk import ToolDescriptor
from tether.secrets.keyring import KeyFamily, KeyRing
from tether.storage.repos.approvals import ApprovalsRepository
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import (
    NON_TERMINAL,
    JobsRepository,
    ModelCallsRepository,
    PolicyVersionsRepository,
    RunsRepository,
)
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

_log = get_logger(__name__)
# The metric label for a proposed tool that is not registered: model text never becomes a label.
_UNREGISTERED = "unregistered"
# The typed terminal reason of a run that failed closed on a replay divergence (ADR-0003 rule 4).
REPLAY_DIVERGENCE_REASON = "replay_divergence"
# The typed not_applied content when a call's pinned version is gone before its ledger row
# exists (ADR-0011 rule 3, ADR-0021 rule 15).
TOOL_VERSION_UNAVAILABLE = "tool version unavailable"
# PD-33: the outcome reason of a write that reported success with output violating its schema.
# The entry is ``completed``/``applied`` with no payload; the model sees only the marker.
OUTPUT_INVALID = "applied: output invalid"
# The typed terminal reason of a run ended by a read configured with fail_run (ADR-0021 rule 9).
TOOL_EXHAUSTED_PREFIX = "tool_exhausted:"
# The typed terminal reasons of the repair/denial bound (W3.4, ADR-0016 rule 4, PD-15).
REPAIR_BUDGET_EXHAUSTED = "repair_budget_exhausted"
DENIAL_BUDGET_EXHAUSTED = "denial_budget_exhausted"
# The invalid reason of a call whose arguments the adapter could not parse. The raw text is
# never carried, persisted or logged (ADR-0017).
MALFORMED_ARGUMENTS = "arguments: malformed"
# TX5's typed result when the current policy now denies a decided call: a policy denial.
_POLICY_NOW_DENY = "policy now: deny"
# The model-visible outcome of an entry resolved by verification (ADR-0019 rules 10, 12).
_VERIFIED_OUTCOMES = {
    LedgerResolution.VERIFIED_APPLIED: CallOutcome.APPLIED,
    LedgerResolution.VERIFIED_NOT_APPLIED: CallOutcome.NOT_APPLIED,
}
# The pause before a provider retry; a module attribute so tests can observe the delays.
_sleep = asyncio.sleep


class RunState(TypedDict, total=False):
    history: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    current: dict[str, Any] | None
    decision: dict[str, Any] | None
    ledger_id: str | None
    approval_id: str | None
    result: dict[str, Any] | None
    # A replay divergence found by the node that set it (ADR-0003 rule 4); routes to TX-RD.
    divergence: dict[str, Any] | None
    # A terminating result's typed reason (ADR-0012 rule 6), e.g. ``tool_exhausted:<tool>``.
    terminating: str | None
    # The repair/denial bound's counters (W3.4). Changed only by node return values, so a
    # replayed node cannot count twice. Absent in checkpoints from before W3.4: read as 0.
    repairs: int
    denials: int
    next_step_ordinal: int
    turns: int
    final_text: str | None
    end_status: str | None


@dataclass(frozen=True)
class RunContext:
    engine: AsyncEngine
    keys: KeyRing
    workspace: WorkspaceConfig
    registry: ToolRegistry
    provider: ModelProvider
    scope: WorkspaceScope
    run_id: uuid.UUID
    agent_id: str
    generation: int
    max_turns: int
    # The default approval expiry, for a tool whose policy rule sets none (W4.2).
    approval_ttl_seconds: int
    # The workspace's policy engine (ADR-0020), built from the same loaded configuration.
    policy: PolicyEngine
    # Provider attempts per model turn (W3.3): retryable errors only, PD-13 backoff between.
    provider_max_attempts: int
    # The repair and denial bound (W3.4, PD-15): one more than this ends the run.
    max_repairs: int
    max_denials: int


def initial_state(system_prompt: str, task_input: dict[str, Any]) -> RunState:
    message = task_input.get("message")
    text = message if isinstance(message, str) else json.dumps(task_input, sort_keys=True)
    return {
        "history": [
            NeutralMessage(role="system", content=system_prompt).model_dump(mode="json"),
            NeutralMessage(role="user", content=text).model_dump(mode="json"),
        ],
        "pending": [],
        "current": None,
        "decision": None,
        "ledger_id": None,
        "approval_id": None,
        "result": None,
        "divergence": None,
        "repairs": 0,
        "denials": 0,
        "next_step_ordinal": 0,
        "turns": 0,
        "final_text": None,
        "end_status": None,
    }


def _validate(descriptor: ToolDescriptor, arguments: dict[str, Any]) -> BaseModel | list[str]:
    """Validated input, or error locations and types only (input values never leak)."""
    try:
        return descriptor.input_model.model_validate(arguments)
    except ValidationError as error:
        return [
            f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['type']}"
            for e in error.errors(include_input=False, include_url=False)
        ]


def call_hash_for(
    keys: KeyRing,
    run: Any,
    descriptor: ToolDescriptor,
    args: BaseModel,
) -> str:
    """The keyed call hash (ADR-0010) under the run's pinned binding key. ``run`` is its row.

    The envelope's ``preconditions`` holds each declared precondition name with its argument
    value (ADR-0021 rule 5); it is empty, as before W3.2, for a tool that declares none.
    """
    arguments = args.model_dump(mode="json")
    envelope = CallEnvelope(
        workspace_id=WorkspaceId(run["workspace_id"]),
        run_id=RunId(str(run["id"])),
        agent_id=AgentId(run["agent_id"]),
        tool=descriptor.name,
        tool_version=descriptor.version,
        arguments=arguments,
        preconditions={name: arguments[name] for name in descriptor.preconditions},
    )
    key = keys.key(KeyFamily.BINDING, run["binding_key_id"]).secret.reveal()
    return call_hash(envelope, key=key, write=descriptor.effect == "write")


def _resolve(ctx: RunContext, call: dict[str, Any]) -> tuple[ToolDescriptor, BaseModel] | None:
    """The offered descriptor and validated arguments of ``call``, or ``None`` if invalid."""
    if call.get("malformed"):
        return None  # no arguments to validate: never resolvable (W3.4)
    descriptor = ctx.registry.offered(call["tool"])
    if descriptor is None:
        return None
    args = _validate(descriptor, call["arguments"])
    return None if isinstance(args, list) else (descriptor, args)


def _offered_hash(ctx: RunContext, run: Any, call: dict[str, Any]) -> str | None:
    """The call's hash under the currently offered version; ``None`` if it is invalid. Used only
    to re-check a call recorded as invalid at TX2, which pinned no version."""
    resolved = _resolve(ctx, call)
    return None if resolved is None else call_hash_for(ctx.keys, run, *resolved)


def _pinned_hash(
    ctx: RunContext, run: Any, call: dict[str, Any], descriptor: ToolDescriptor
) -> str | None:
    """The call's hash under its pinned descriptor; ``None`` if the arguments do not validate."""
    args = _validate(descriptor, call["arguments"])
    return None if isinstance(args, list) else call_hash_for(ctx.keys, run, descriptor, args)


def _masked_preconditions(descriptor: ToolDescriptor, args: BaseModel) -> dict[str, Any]:
    """The declared precondition values, masked like every argument (ADR-0017, ADR-0021)."""
    masked = mask(args)
    return {name: masked[name] for name in descriptor.preconditions}


def _hashes_match(stored: object, recomputed: str | None) -> bool:
    """Hash-checked create-or-get (ADR-0003 rule 4): a persisted row or decision may be adopted
    only for the call it was recorded for. Two invalid calls (no hash) are consistent."""
    if stored is None and recomputed is None:
        return True
    return call_hashes_equal(stored, recomputed)


def _divergence(
    call: dict[str, Any], *, path: str, stored: object, recomputed: str | None
) -> RunState:
    """The state update that routes a node to TX-RD. Hashes only: no arguments, no payloads."""
    return {
        "divergence": {
            "step_ordinal": call["step_ordinal"],
            "tool": call["tool"],
            "path": path,
            "stored_call_hash": stored if isinstance(stored, str) else None,
            "recomputed_call_hash": recomputed,
        }
    }


def _action_request(
    ctx: RunContext, run: Any, descriptor: ToolDescriptor, ordinal: int, call_hash_hex: str
) -> ActionRequest:
    """The trusted policy input for one call (ADR-0020): the run's requester snapshot, agent and
    workspace, the registry descriptor and runtime metadata. Never the model's arguments or
    text; ``trusted_attributes`` stays empty in V1."""
    return ActionRequest(
        workspace_id=ctx.scope.workspace_id,
        requester=TrustedRequester.from_snapshot(run["requester_snapshot"]),
        agent=TrustedAgent(str(run["agent_id"])),
        tool=ToolPolicyMetadata.from_descriptor(descriptor),
        context=ActionContext(
            run_id=str(ctx.run_id), step_ordinal=ordinal, call_hash=call_hash_hex
        ),
    )


async def _assert_current_policy(uow: UnitOfWork, ctx: RunContext) -> None:
    active = await uow.repository(PolicyVersionsRepository, ctx.scope).active_version()
    if active != ctx.policy.policy_version:
        raise PolicyStaleError(f"workspace {ctx.scope.workspace_id} policy is {active}")


def _required[T](value: T | None, what: str) -> T:
    if value is None:
        raise RuntimeError(f"run graph invariant violated: {what} is missing")
    return value


def _result(outcome: CallOutcome, content: Any) -> dict[str, Any]:
    return {"outcome": outcome.value, "content": content}


def _result_from_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    decision = _required(decision, "decision")
    if decision["decision"] == "invalid":
        return _result(CallOutcome.INVALID, decision["reasons"])
    return _result(CallOutcome.DENIED, decision["reasons"])


def _tool_message(call_id: str, outcome: str, content: Any) -> dict[str, Any]:
    text = content if isinstance(content, str) else json.dumps(content, sort_keys=True)
    message = NeutralMessage(
        role="tool", call_id=call_id, outcome=CallOutcome(outcome), content=text
    )
    return message.model_dump(mode="json")


def _record_decision(uow: UnitOfWork, current: Any, decision: str, tool: str, step: int) -> None:
    """Telemetry for a newly recorded decision, emitted only once TX2 commits."""
    tracing.annotate(current, **{"tether.policy.decision": decision})

    def committed() -> None:
        metrics.policy_decision(decision, tool)
        _log.info("policy decision", decision=decision, tool=tool, step=step)

    uow.on_commit(committed)


async def reconciled_outcomes(
    uow: UnitOfWork, scope: WorkspaceScope, run_id: uuid.UUID
) -> list[str]:
    """Every reconciled or verified outcome of the run, for its terminal summary (ADR-0019 rule
    9: a verified outcome after a stop is named, never reported as clean)."""
    entries = await uow.repository(LedgerRepository, scope).for_run(run_id)
    return [
        f"{entry['tool']} {entry['resolution']}"
        for entry in entries
        if str(entry["resolution"] or "").startswith(("reconciled_", "verified_"))
    ]


async def stop_ending(uow: UnitOfWork, scope: WorkspaceScope, run: Any) -> tuple[RunStatus, str]:
    """How a stopped run ends: ``cancelled`` or ``timed_out`` (ADR-0006), with a summary that
    names every reconciled outcome, so a reconciled stop is never reported as clean."""
    status = RunStatus(run["stop_status"])
    reason = (
        f"cancelled by {run['cancel_requested_by']}"
        if status == RunStatus.CANCELLED
        else "deadline passed"
    )
    return status, "; ".join([reason, *await reconciled_outcomes(uow, scope, run["id"])])


def build_graph(ctx: RunContext) -> StateGraph[RunState]:
    async def reconciled(uow: UnitOfWork) -> list[str]:
        return await reconciled_outcomes(uow, ctx.scope, ctx.run_id)

    async def stopped(uow: UnitOfWork, run: Any) -> RunState:
        status, summary = await stop_ending(uow, ctx.scope, run)
        return {"end_status": status, "final_text": summary}

    async def check_pinned(
        uow: UnitOfWork,
        run: Any,
        call: dict[str, Any],
        *,
        tool: object,
        version: object,
        stored: object,
        path: str,
        decided: object = None,
    ) -> RunState | None:
        """Hash-checked adoption under the call's pinned version (ADR-0003 rule 4, ADR-0011,
        ADR-0021 rule 14). ``None``: the persisted row is for this exact call.

        The pinned ``(tool, version)`` comes from the persisted row, never from what is offered
        now. If that version no longer resolves and no ledger row exists for the call, the call
        gets a typed ``not_applied`` and nothing is sent (ADR-0011 rule 3); it is not a
        divergence. Any other mismatch is a divergence.

        Once a ledger row exists, a version gone is a deployment, not a divergence (ADR-0011 rule
        5, ADR-0021 rule 15): the ledger entry is authoritative, the worker records its honest
        outcome and wakes the run, and the run follows that outcome. Without the descriptor the
        hash cannot be recomputed from the arguments, so the persisted rows must agree instead:
        the ledger row has this call's tool and pinned version and the same call hash as the
        adopted row and, when this lineage holds one, as its own decision (``decided``).
        """
        if tool != call["tool"] or not isinstance(version, str):
            return _divergence(call, path=path, stored=stored, recomputed=None)
        descriptor = ctx.registry.resolve(call["tool"], version)
        if descriptor is None:
            ledger = uow.repository(LedgerRepository, ctx.scope)
            entry = await ledger.by_ordinal(ctx.run_id, call["step_ordinal"])
            if entry is None:
                return {"result": _result(CallOutcome.NOT_APPLIED, TOOL_VERSION_UNAVAILABLE)}
            if (
                entry["tool"] == call["tool"]
                and entry["tool_version"] == version
                and call_hashes_equal(entry["call_hash"], stored)
                and (decided is None or call_hashes_equal(entry["call_hash"], decided))
            ):
                return None
            return _divergence(call, path=path, stored=stored, recomputed=None)
        recomputed = _pinned_hash(ctx, run, call, descriptor)
        if _hashes_match(stored, recomputed):
            return None
        return _divergence(call, path=path, stored=stored, recomputed=recomputed)

    async def before_attempt() -> RunState | None:
        """G0, fenced: the stop predicate and the divergence gate before every provider attempt
        (§9.2, ADR-0006). ``None``: the attempt may be made."""
        async with UnitOfWork(ctx.engine) as uow:
            run = await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
            if run["stopped"]:
                return await stopped(uow, run)
            # A recorded divergence is authoritative: no further model call, whatever the
            # checkpoint lineage says (ADR-0005 rule 6). The run drains and fails closed.
            if await uow.repository(AuditRepository, ctx.scope).has_event(
                ctx.run_id, "REPLAY_DIVERGENCE"
            ):
                return {"divergence": {"path": "model_gate", "recorded": True}}
        return None

    async def attempt_model(
        state: RunState, history: list[NeutralMessage], attempt: int
    ) -> tuple[ModelTurn | ProviderError, RunState | None]:
        """One provider attempt and its own ``model_calls`` row (ADR-0016 rule 5, §9.2): the
        turn or the normalised error, and the stop found right after the response, if any."""
        turn: ModelTurn | None = None
        failed: ProviderError | None = None
        with tracing.span(
            "model.call",
            attributes={
                "tether.model.provider": ctx.provider.name,
                "tether.model.name": ctx.provider.model,
                "tether.model.turn": state["turns"],
                "tether.model.attempt": attempt,
            },
        ) as current:
            started = time.monotonic()
            try:
                turn = await ctx.provider.complete(history, ctx.registry.manifest())
            except ProviderError as error:
                failed = error
                tracing.mark_error(current, type(error).__name__)
            outcome = "failed" if turn is None else "succeeded"
            metrics.model_call(ctx.provider.name, outcome, time.monotonic() - started)
            tracing.annotate(
                current,
                **{
                    "tether.model.outcome": outcome,
                    "tether.model.tool_calls": 0 if turn is None else len(turn.tool_calls),
                },
            )
        if failed is not None:
            _log.warning(
                "model call failed",
                provider=ctx.provider.name,
                turn=state["turns"],
                attempt=attempt,
                code=failed.code,
            )
        async with UnitOfWork(ctx.engine) as uow:
            # Usage as the provider reported it, or nothing: it is never estimated, and a field
            # the provider omitted stays null. Dollar cost is deferred (D-7), so `cost_usd` is
            # left unwritten rather than derived from a price table.
            usage = turn.usage if turn is not None else None
            await uow.repository(ModelCallsRepository, ctx.scope).add(
                {
                    "id": uuid.uuid4(),
                    "run_id": ctx.run_id,
                    "turn_ordinal": state["turns"],
                    "provider_role": "primary",
                    "provider": ctx.provider.name,
                    "model": ctx.provider.model,
                    # The attempt within this execution of the node: after a crash the node
                    # re-runs from attempt 1 and every attempt still gets its own row.
                    "attempt": attempt,
                    "occurrence_id": uuid.uuid4(),
                    "status": "failed" if turn is None else "succeeded",
                    "input_tokens": usage.input_tokens if usage is not None else None,
                    "output_tokens": usage.output_tokens if usage is not None else None,
                    # The provider's normalised, label-safe code, never its own error prose,
                    # which can quote the request.
                    "error_code": failed.code if failed is not None else None,
                    "started_at": func.clock_timestamp(),
                    "finished_at": func.clock_timestamp(),
                }
            )
            # A stop committed while the request was in flight: the response is recorded for
            # cost above, its proposals are never governed and a failure is not retried (§9.2).
            run = await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
            answer: ModelTurn | ProviderError = (
                turn if turn is not None else _required(failed, "provider error")
            )
            if run["stopped"]:
                return answer, await stopped(uow, run)
        return answer, None

    async def call_model(state: RunState) -> RunState:
        gated = await before_attempt()
        if gated is not None:
            return gated
        if state["turns"] >= ctx.max_turns:
            return {"end_status": RunStatus.FAILED, "final_text": "model turn budget exhausted"}
        history = [NeutralMessage.model_validate(m) for m in state["history"]]
        attempt = 1
        while True:
            answer, ended = await attempt_model(state, history, attempt)
            if ended is not None:
                return ended
            if isinstance(answer, ModelTurn):
                turn = answer
                break
            # Bounded retry on retryable errors only; no fallback in V1 (ADR-0016 rule 3).
            if not answer.retryable or attempt >= ctx.provider_max_attempts:
                failure = f"model call failed: {answer}"
                return {"end_status": RunStatus.FAILED, "final_text": failure}
            await _sleep(backoff.tool_backoff_seconds(attempt))  # PD-13
            attempt += 1
            gated = await before_attempt()
            if gated is not None:
                return gated
        first = state["next_step_ordinal"]
        pending = [
            {"step_ordinal": first + i, **call.model_dump(mode="json")}
            for i, call in enumerate(turn.tool_calls)
        ]
        assistant = NeutralMessage(role="assistant", content=turn.text, tool_calls=turn.tool_calls)
        return {
            "history": [*state["history"], assistant.model_dump(mode="json")],
            "pending": pending,
            "next_step_ordinal": first + len(pending),
            "turns": state["turns"] + 1,
            "final_text": None if pending else turn.text,
        }

    def after_model(state: RunState) -> str:
        if state.get("divergence"):
            return "drain_divergence"
        if state.get("end_status") or not state["pending"]:
            return "finalize"
        return "next_call"

    async def next_call(state: RunState) -> RunState:
        pending = state["pending"]
        if not pending:
            return {"current": None}
        return {
            "current": pending[0],
            "pending": pending[1:],
            "decision": None,
            "ledger_id": None,
            "approval_id": None,
            "result": None,
            "divergence": None,
        }

    def after_next(state: RunState) -> str:
        return "govern_call" if state.get("current") else "call_model"

    async def govern_call(state: RunState) -> RunState:
        """TX2: adopt a persisted decision, or validate, evaluate policy and record it."""
        call = state["current"]
        call = _required(call, "current call")
        dedupe = f"decision:{call['step_ordinal']}"
        with tracing.span(
            "policy.govern",
            attributes={"tether.tool": call["tool"], "tether.step": call["step_ordinal"]},
        ) as current:
            async with UnitOfWork(ctx.engine) as uow:
                run = await uow.repository(RunsRepository, ctx.scope).guard(
                    ctx.run_id, ctx.generation
                )
                audit = uow.repository(AuditRepository, ctx.scope)
                adopted = await audit.find(ctx.run_id, dedupe)
                if adopted is not None:
                    # Hash-checked adoption (ADR-0003 rule 4): the recorded decision is for
                    # this exact call, or this replay has diverged from what was persisted.
                    stored = adopted.payload.get("call_hash")
                    if stored is None:
                        # Recorded as invalid: no version was pinned (ADR-0021 rule 14).
                        recomputed = _offered_hash(ctx, run, call)
                        if adopted.tool != call["tool"] or not _hashes_match(stored, recomputed):
                            tracing.annotate(current, **{"tether.replay.divergence": True})
                            return _divergence(
                                call, path="decision", stored=stored, recomputed=recomputed
                            )
                    else:
                        found = await check_pinned(
                            uow,
                            run,
                            call,
                            tool=adopted.tool,
                            version=adopted.payload.get("tool_version"),
                            stored=stored,
                            path="decision",
                        )
                        if found is not None:
                            if found.get("divergence"):
                                tracing.annotate(current, **{"tether.replay.divergence": True})
                            return {"decision": adopted.payload, **found}
                    tracing.annotate(
                        current,
                        **{
                            "tether.policy.decision": adopted.payload.get("decision"),
                            "tether.policy.adopted": True,
                        },
                    )
                    return {"decision": adopted.payload}
                if run["stopped"]:
                    return await stopped(uow, run)
                descriptor = ctx.registry.offered(call["tool"])
                args: BaseModel | list[str]
                if call.get("malformed"):
                    # Unparseable arguments (ADR-0016 rule 4): a typed invalid result, a repair.
                    args = [MALFORMED_ARGUMENTS]
                elif descriptor is not None:
                    args = _validate(descriptor, call["arguments"])
                else:
                    args = ["tool is not offered to this agent"]
                if descriptor is None or isinstance(args, list):
                    payload: dict[str, Any] = {
                        "decision": "invalid",
                        "reasons": args if isinstance(args, list) else [],
                        "call_hash": None,
                    }
                    await audit.append(
                        ctx.keys,
                        run_id=ctx.run_id,
                        dedupe_key=dedupe,
                        event_type="CALL_INVALID",
                        payload=payload,
                        tool=call["tool"],
                    )
                    label = descriptor.name if descriptor is not None else _UNREGISTERED
                    _record_decision(uow, current, "invalid", label, call["step_ordinal"])
                    return {"decision": payload}
                await _assert_current_policy(uow, ctx)
                decided_hash = call_hash_for(ctx.keys, run, descriptor, args)
                decision = ctx.policy.evaluate_action(
                    _action_request(ctx, run, descriptor, call["step_ordinal"], decided_hash)
                )
                payload = {
                    "decision": decision.outcome,
                    "reasons": list(decision.reasons),
                    "call_hash": decided_hash,
                    "tool_version": descriptor.version,
                    "approver_roles": list(decision.approver_roles),
                }
                await audit.append(
                    ctx.keys,
                    run_id=ctx.run_id,
                    dedupe_key=dedupe,
                    event_type="POLICY_DECISION",
                    payload=payload,
                    tool=descriptor.name,
                    policy_version=decision.policy_version,
                )
                _record_decision(
                    uow, current, decision.outcome, descriptor.name, call["step_ordinal"]
                )
                return {"decision": payload}

    def after_govern(state: RunState) -> str:
        if state.get("divergence"):
            return "record_divergence"
        if state.get("end_status"):
            return "finalize"
        if state.get("result"):
            return "append_result"  # the pinned version is gone (ADR-0021 rule 15)
        decision = state["decision"]
        decision = _required(decision, "decision")
        if decision["decision"] == "allow":
            return "authorize_call"
        if decision["decision"] == "require_approval":
            return "request_approval"
        return "append_result"

    async def request_approval(state: RunState) -> RunState:
        """TX3: create-or-get the approval request bound to the decided call hash."""
        call = _required(state["current"], "current call")
        decided = _required(state["decision"], "decision")
        ordinal = call["step_ordinal"]
        async with UnitOfWork(ctx.engine) as uow:
            approvals = uow.repository(ApprovalsRepository, ctx.scope)
            run = await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
            existing = await approvals.by_ordinal(ctx.run_id, ordinal)
            if existing is not None:
                # Hash-checked get (ADR-0003 rule 4): the request must be for this exact call,
                # under the version it pinned.
                found = await check_pinned(
                    uow,
                    run,
                    call,
                    tool=existing["tool"],
                    version=existing["tool_version"],
                    stored=existing["call_hash"],
                    path="approval",
                    decided=decided["call_hash"],
                )
                return found if found is not None else {"approval_id": str(existing["id"])}
            if run["stopped"]:
                return await stopped(uow, run)
            # The version the decision pinned, never the one offered now (ADR-0021 rule 14).
            descriptor = ctx.registry.resolve(call["tool"], decided["tool_version"])
            if descriptor is None:
                return {"result": _result(CallOutcome.NOT_APPLIED, TOOL_VERSION_UNAVAILABLE)}
            args = _validate(descriptor, call["arguments"])
            if isinstance(args, list):
                raise RuntimeError("a decided call no longer validates")
            approval_id = uuid.uuid4()
            masked = mask(args)
            preconditions = _masked_preconditions(descriptor, args)
            # The tool's own expiry from its policy rule, else the default (W4.2); both were
            # checked against the maximum at worker startup. Decision-time checks are unchanged.
            seconds = ctx.workspace.policy.approval_expiry_for(
                descriptor.name, ctx.approval_ttl_seconds
            )
            expiry = func.clock_timestamp() + func.make_interval(0, 0, 0, 0, 0, 0, seconds)
            await approvals.add(
                {
                    "id": approval_id,
                    "run_id": ctx.run_id,
                    "step_ordinal": ordinal,
                    "call_hash": decided["call_hash"],
                    "tool": descriptor.name,
                    "tool_version": descriptor.version,
                    "arguments_masked": masked,
                    "preconditions_masked": preconditions,
                    "expires_at": expiry,
                }
            )
            requested: dict[str, Any] = {
                "approval_id": str(approval_id),
                "call_hash": decided["call_hash"],
                "arguments": masked,
            }
            if preconditions:
                requested["preconditions"] = preconditions
            await uow.repository(AuditRepository, ctx.scope).append(
                ctx.keys,
                run_id=ctx.run_id,
                dedupe_key=f"approval_requested:{ordinal}",
                event_type="APPROVAL_REQUESTED",
                payload=requested,
                tool=descriptor.name,
            )
            # TX3's timer (TX-EA). The same expression, evaluated after the insert, makes it due
            # no earlier than the stored expiry.
            await uow.repository(JobsRepository, ctx.scope).enqueue(
                JobKind.EXPIRE_APPROVAL, ctx.run_id, ref_id=approval_id, run_after=expiry
            )
            tracing.event("approval.requested", **{"tether.tool": descriptor.name})
            uow.on_commit(
                lambda: _log.info("approval requested", tool=descriptor.name, step=ordinal)
            )
        return {"approval_id": str(approval_id)}

    def after_request(state: RunState) -> str:
        if state.get("divergence"):
            return "record_divergence"
        if state.get("end_status"):
            return "finalize"
        return "append_result" if state.get("result") else "await_approval"

    async def await_approval(state: RunState) -> RunState:
        """Wait node: re-read the approval; interrupt while it is pending."""
        call = _required(state["current"], "current call")
        approval_id = uuid.UUID(_required(state["approval_id"], "approval id"))
        decided_hash = _required(state["decision"], "decision").get("call_hash")
        while True:
            async with UnitOfWork(ctx.engine) as uow:
                row = await uow.repository(ApprovalsRepository, ctx.scope).get(approval_id)
                run = await uow.repository(RunsRepository, ctx.scope).get(ctx.run_id)
                approval = _required(row, "approval")
                # The row this lineage waits on must be the request for this exact call, under
                # the version it pinned (ADR-0021 rule 14).
                if approval["step_ordinal"] != call["step_ordinal"]:
                    return _divergence(
                        call, path="approval_wait", stored=approval["call_hash"], recomputed=None
                    )
                found = await check_pinned(
                    uow,
                    _required(run, "run"),
                    call,
                    tool=approval["tool"],
                    version=approval["tool_version"],
                    stored=approval["call_hash"],
                    path="approval_wait",
                    decided=decided_hash,
                )
            if found is not None:
                return found
            if approval["status"] == "approved":
                return {}
            if approval["status"] == "rejected":
                return {"result": _result(CallOutcome.REJECTED, "rejected by approver")}
            if approval["status"] in ("expired", "closed"):
                return {"result": _result(CallOutcome.EXPIRED, f"approval {approval['status']}")}
            interrupt("awaiting_approval")

    def after_await_approval(state: RunState) -> str:
        if state.get("divergence"):
            return "record_divergence"
        return "append_result" if state.get("result") else "authorize_call"

    async def authorize_call(state: RunState) -> RunState:
        """TX5 in a span; the outcome (authorized, or why not) is the span's attribute."""
        call = _required(state["current"], "current call")
        with tracing.span(
            "tool.authorize",
            attributes={"tether.tool": call["tool"], "tether.step": call["step_ordinal"]},
        ) as current:
            update = await _authorize(state)
            result = update.get("result")
            if result is not None:
                outcome = str(result["outcome"])
                _log.info("call not authorized", outcome=outcome, step=call["step_ordinal"])
            elif update.get("divergence"):
                outcome = "replay_divergence"
            else:
                outcome = "stopped" if update.get("end_status") else "authorized"
            tracing.annotate(current, **{"tether.authorization": outcome})
            return update

    async def _authorize(state: RunState) -> RunState:
        """TX5: ledger-first; else re-evaluate current policy and create the ledger entry."""
        call, decided = state["current"], state["decision"]
        call = _required(call, "current call")
        decided = _required(decided, "decision")
        ordinal = call["step_ordinal"]
        async with UnitOfWork(ctx.engine) as uow:
            ledger = uow.repository(LedgerRepository, ctx.scope)
            run = await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
            existing = await ledger.by_ordinal(ctx.run_id, ordinal)
            if existing is not None:
                # Ledger-first, hash-checked (ADR-0003 rules 4 and 6): the row is adopted only
                # for the exact call it authorized, under its pinned version; otherwise this
                # replay has diverged.
                found = await check_pinned(
                    uow,
                    run,
                    call,
                    tool=existing["tool"],
                    version=existing["tool_version"],
                    stored=existing["call_hash"],
                    path="ledger",
                    decided=decided["call_hash"],
                )
                return found if found is not None else {"ledger_id": str(existing["id"])}
            if run["stopped"]:
                return await stopped(uow, run)
            # Exactly the version the decision pinned; never a newer one (ADR-0011 rule 3).
            descriptor = ctx.registry.resolve(call["tool"], decided["tool_version"])
            if descriptor is None:
                return {"result": _result(CallOutcome.NOT_APPLIED, TOOL_VERSION_UNAVAILABLE)}
            args = _validate(descriptor, call["arguments"])
            if isinstance(args, list):
                return {"result": _result(CallOutcome.NOT_APPLIED, "tool no longer resolvable")}
            await _assert_current_policy(uow, ctx)
            hashed = call_hash_for(ctx.keys, run, descriptor, args)
            # TX5: the same trusted inputs, re-read under the current policy (ADR-0009 rule 3).
            # The descriptor is the pinned one: its risk and permissions are version-bound, and
            # revocation of a pinned action goes through the current policy (ADR-0021 rule 14).
            decision = ctx.policy.evaluate_action(
                _action_request(ctx, run, descriptor, ordinal, hashed)
            )
            if not call_hashes_equal(hashed, decided["call_hash"]):
                return {"result": _result(CallOutcome.DENIED, "call changed since its decision")}
            if decision.outcome == "deny":
                return {"result": _result(CallOutcome.DENIED, _POLICY_NOW_DENY)}
            if decision.outcome == "require_approval":
                # TX5 mapping: an approved, unexpired approval bound to this exact call hash.
                approval = await uow.repository(ApprovalsRepository, ctx.scope).by_ordinal(
                    ctx.run_id, ordinal
                )
                if (
                    approval is None
                    or approval["status"] != "approved"
                    or not call_hashes_equal(approval["call_hash"], hashed)
                ):
                    return {"result": _result(CallOutcome.DENIED, "no matching approval")}
                if not approval["live"]:
                    return {"result": _result(CallOutcome.EXPIRED, "approval lapsed")}
            entry_id = uuid.uuid4()
            key = idempotency_key(RunId(str(ctx.run_id)), ordinal, hashed)
            await ledger.add(
                {
                    "id": entry_id,
                    "run_id": ctx.run_id,
                    "step_ordinal": ordinal,
                    "tool": descriptor.name,
                    "tool_version": descriptor.version,
                    "effect": descriptor.effect,
                    "idempotency_support": (
                        "non_idempotent"
                        if descriptor.idempotency == "unsupported"
                        else "idempotent"
                    ),
                    "call_hash": hashed,
                    "idempotency_key": key,
                }
            )
            authorized: dict[str, Any] = {
                "ledger_id": str(entry_id),
                "call_hash": hashed,
                "idempotency_key": key,
            }
            if descriptor.preconditions:
                authorized["preconditions"] = _masked_preconditions(descriptor, args)
            await uow.repository(AuditRepository, ctx.scope).append(
                ctx.keys,
                run_id=ctx.run_id,
                dedupe_key=f"authorized:{ordinal}",
                event_type="EXECUTION_AUTHORIZED",
                payload=authorized,
                tool=descriptor.name,
                policy_version=decision.policy_version,
            )
            # The job payload carries the validated arguments (§12); execute_tool re-checks
            # them against the ledger's call hash before dispatching.
            await uow.repository(JobsRepository, ctx.scope).enqueue(
                JobKind.EXECUTE_TOOL,
                ctx.run_id,
                ref_id=entry_id,
                payload={"arguments": args.model_dump(mode="json")},
            )
        return {"ledger_id": str(entry_id)}

    def after_authorize(state: RunState) -> str:
        if state.get("divergence"):
            return "record_divergence"
        if state.get("end_status"):
            return "finalize"
        return "append_result" if state.get("result") else "await_tool"

    async def await_tool(state: RunState) -> RunState:
        """Wait node: re-read the ledger entry; interrupt until it is terminal."""
        call = _required(state["current"], "current call")
        ledger_id = state["ledger_id"]
        ledger_id = _required(ledger_id, "ledger entry id")
        decided_hash = _required(state["decision"], "decision").get("call_hash")
        while True:
            async with UnitOfWork(ctx.engine) as uow:
                entry = await uow.repository(LedgerRepository, ctx.scope).get(uuid.UUID(ledger_id))
                run = await uow.repository(RunsRepository, ctx.scope).get(ctx.run_id)
                entry = _required(entry, "ledger entry")
                # The outcome this lineage waits on must belong to this exact call, under its
                # pinned version: a divergent replay can never attach one call's outcome to
                # another call (AD-10).
                if entry["step_ordinal"] != call["step_ordinal"]:
                    return _divergence(
                        call, path="ledger_wait", stored=entry["call_hash"], recomputed=None
                    )
                found = await check_pinned(
                    uow,
                    _required(run, "run"),
                    call,
                    tool=entry["tool"],
                    version=entry["tool_version"],
                    stored=entry["call_hash"],
                    path="ledger_wait",
                    decided=decided_hash,
                )
            if found is not None:
                return found
            resolution = entry["resolution"]
            if resolution in _VERIFIED_OUTCOMES:
                # Resolved by a definitive read-back (ADR-0019 rule 10, invariant 37): the write's
                # own output is unknown and not fabricated; the model gets the masked evidence
                # with the verification marker.
                return {
                    "result": _result(
                        _VERIFIED_OUTCOMES[resolution],
                        {
                            "resolution": resolution,
                            "verified": True,
                            "evidence": entry["verification_evidence_masked"],
                        },
                    )
                }
            if entry["state"] == "completed":
                if resolution == LedgerResolution.RECONCILED_APPLIED:
                    # A human confirmed the write happened; its real output is unknown.
                    return {"result": _result(CallOutcome.APPLIED, {"resolution": resolution})}
                if entry["outcome_reason"] == OUTPUT_INVALID:
                    # PD-33: applied, with a marker and no payload.
                    return {"result": _result(CallOutcome.APPLIED, {"output_invalid": True})}
                return {"result": _result(CallOutcome.APPLIED, entry["outcome_payload_masked"])}
            if entry["state"] == "failed":
                if resolution == LedgerResolution.RECONCILED_NOT_APPLIED:
                    return {"result": _result(CallOutcome.NOT_APPLIED, {"resolution": resolution})}
                outcome_class = entry["outcome_class"]
                if outcome_class == OutcomeClass.NOT_APPLIED:
                    # Positive evidence the action did not apply (ADR-0021 rule 11).
                    return {"result": _result(CallOutcome.NOT_APPLIED, entry["outcome_reason"])}
                pinned = ctx.registry.resolve(entry["tool"], entry["tool_version"])
                if (
                    outcome_class == OutcomeClass.UNKNOWN
                    and pinned is not None
                    and pinned.on_exhausted == "fail_run"
                ):
                    # A terminating result (ADR-0012 rule 6, ADR-0021 rules 8-9): no history,
                    # no skip events, no further model call.
                    return {"terminating": f"{TOOL_EXHAUSTED_PREFIX}{entry['tool']}"}
                # ``unknown`` and ``failed``, and unclassified historical rows (before 0004).
                return {"result": _result(CallOutcome.FAILED, entry["outcome_reason"])}
            if entry["state"] == "abandoned":
                # ADR-0007: abandon terminates the run; no further model call (TX11).
                return {
                    "end_status": RunStatus.ABANDONED,
                    "final_text": f"{entry['tool']} abandoned by reconciliation",
                }
            waiting = entry["state"] == "indeterminate"
            interrupt("awaiting_reconciliation" if waiting else "awaiting_tool")

    def after_await_tool(state: RunState) -> str:
        if state.get("divergence"):
            return "record_divergence"
        if state.get("terminating"):
            return "drain_terminating"
        return "finalize" if state.get("end_status") else "append_result"

    async def drain_terminating(state: RunState) -> RunState:
        """Wait node for a terminating result (ADR-0012 rule 6, ADR-0021 rule 9).

        External truth first: while any ledger entry of the run is unresolved, the run is held
        for reconciliation (ADR-0018) and nothing is terminalized. Then the established rules
        decide: an active stop ends it ``cancelled``/``timed_out`` (ADR-0006), with the reason in
        the summary; otherwise it ends ``failed`` with the reason and any reconciled outcome.
        """
        reason = _required(state.get("terminating"), "terminating reason")
        while True:
            async with UnitOfWork(ctx.engine) as uow:
                held = await uow.repository(LedgerRepository, ctx.scope).unresolved(ctx.run_id)
                if not held:
                    run = await uow.repository(RunsRepository, ctx.scope).share_lock(ctx.run_id)
                    if run["stopped"]:
                        ended = await stopped(uow, run)
                        return {**ended, "final_text": f"{ended['final_text']}; {reason}"}
                    summary = "; ".join([reason, *await reconciled(uow)])
                    return {"end_status": RunStatus.FAILED, "final_text": summary}
            interrupt("awaiting_reconciliation")

    async def record_divergence(state: RunState) -> RunState:
        """TX-RD: the replay divergence as a fenced, create-or-get decision-class audit event.

        Nothing else is written: no decision, approval or ledger row is created, changed or
        removed, and nothing is authorized or sent. The run then drains (``drain_divergence``)
        and ends ``failed`` with the typed reason ``replay_divergence`` (TX11).
        """
        found = _required(state.get("divergence"), "divergence")
        ordinal = int(found["step_ordinal"])
        payload = {
            key: found.get(key)
            for key in ("step_ordinal", "tool", "path", "stored_call_hash", "recomputed_call_hash")
        }
        with tracing.span(
            "replay.divergence",
            attributes={
                "tether.tool": str(found["tool"]),
                "tether.step": ordinal,
                "tether.replay.path": str(found["path"]),
            },
        ):
            async with UnitOfWork(ctx.engine) as uow:
                await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
                await uow.repository(AuditRepository, ctx.scope).append(
                    ctx.keys,
                    run_id=ctx.run_id,
                    dedupe_key=f"replay_divergence:{ordinal}",
                    event_type="REPLAY_DIVERGENCE",
                    payload=payload,
                    tool=str(found["tool"]),
                )
                uow.on_commit(
                    lambda: _log.error(
                        "replay divergence", step=ordinal, path=found["path"], tool=found["tool"]
                    )
                )
        return {"divergence": {**found, "recorded": True}}

    async def drain_divergence(state: RunState) -> RunState:
        """Wait node: a diverged run holds while any ledger entry is unresolved, then ends.

        An in-flight or ``indeterminate`` entry is real-world truth that a terminal status must
        not hide (ADR-0018): its outcome or reconciliation wakes the run, which then ends. The
        integrity failure never overrides a stop (ADR-0006): a run whose stop predicate is true
        ends ``cancelled``/``timed_out`` through the usual stop path, otherwise ``failed``; the
        summary names ``replay_divergence`` either way, and the audit event stays.
        """
        while True:
            async with UnitOfWork(ctx.engine) as uow:
                held = await uow.repository(LedgerRepository, ctx.scope).unresolved(ctx.run_id)
                if not held:
                    run = await uow.repository(RunsRepository, ctx.scope).share_lock(ctx.run_id)
                    if run["stopped"]:
                        ended = await stopped(uow, run)
                        summary = f"{ended['final_text']}; {REPLAY_DIVERGENCE_REASON}"
                        return {**ended, "final_text": summary}
                    return {
                        "end_status": RunStatus.FAILED,
                        "final_text": REPLAY_DIVERGENCE_REASON,
                    }
            interrupt("awaiting_reconciliation")

    async def record_skips(
        call: dict[str, Any], outcome: CallOutcome, remaining: list[dict[str, Any]]
    ) -> RunState | None:
        """The skip events of ADR-0012 rule 4, fenced, in one transaction; ``None`` once done.

        One decision-class ``CALL_SKIPPED`` per remaining call, create-or-get by ordinal. A
        skipped call is never validated, governed, approved, ledgered or sent: the event holds
        identifiers and the cause only, never arguments. On replay, an existing event is adopted
        only if it records the same tool and the same cause; anything else is a replay
        divergence (W2.2) and nothing is written. If the stop predicate is already true, the
        remaining calls are not marked skipped and the run ends (ADR-0012 rule 6).
        """
        async with UnitOfWork(ctx.engine) as uow:
            run = await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
            if run["stopped"]:
                return await stopped(uow, run)
            audit = uow.repository(AuditRepository, ctx.scope)
            missing = []
            for skipped in remaining:
                payload = {
                    "step_ordinal": skipped["step_ordinal"],
                    "call_id": skipped["call_id"],
                    "reason": "earlier_call_not_applied",
                    "trigger_step_ordinal": call["step_ordinal"],
                    "trigger_call_id": call["call_id"],
                    "trigger_outcome": outcome.value,
                }
                existing = await audit.find(ctx.run_id, f"skipped:{skipped['step_ordinal']}")
                if existing is None:
                    missing.append((skipped, payload))
                    continue
                stored = existing.payload
                if (
                    existing.event_type != "CALL_SKIPPED"
                    or existing.tool != skipped["tool"]
                    or stored.get("trigger_step_ordinal") != payload["trigger_step_ordinal"]
                    or stored.get("trigger_outcome") != payload["trigger_outcome"]
                ):
                    return _divergence(skipped, path="skip", stored=None, recomputed=None)
            for skipped, payload in missing:
                await audit.append(
                    ctx.keys,
                    run_id=ctx.run_id,
                    dedupe_key=f"skipped:{skipped['step_ordinal']}",
                    event_type="CALL_SKIPPED",
                    payload=payload,
                    tool=skipped["tool"],
                )
            if missing:
                skipped_count = len(missing)
                uow.on_commit(
                    lambda: _log.info(
                        "calls skipped",
                        count=skipped_count,
                        step=call["step_ordinal"],
                        outcome=outcome.value,
                    )
                )
        return None

    def bound(state: RunState, result: dict[str, Any]) -> RunState:
        """The repair/denial bound (W3.4, ADR-0016 rule 4, PD-15): the counter this result
        advances, and the terminating reason once a budget is exceeded. Invalid proposals are
        repairs; policy denials (TX2 ``deny``, TX5 ``policy now: deny``) are denials. Human
        rejections, expired approvals and every other result count for nothing."""
        if result["outcome"] == CallOutcome.INVALID:
            repairs = state.get("repairs", 0) + 1
            if repairs > ctx.max_repairs:
                return {"repairs": repairs, "terminating": REPAIR_BUDGET_EXHAUSTED}
            return {"repairs": repairs}
        decided = state.get("result") is None
        if result["outcome"] == CallOutcome.DENIED and (
            decided or result["content"] == _POLICY_NOW_DENY
        ):
            denials = state.get("denials", 0) + 1
            if denials > ctx.max_denials:
                return {"denials": denials, "terminating": DENIAL_BUDGET_EXHAUSTED}
            return {"denials": denials}
        return {}

    async def append_result(state: RunState) -> RunState:
        call = state["current"]
        call = _required(call, "current call")
        result = state.get("result") or _result_from_decision(state["decision"])
        counted = bound(state, result)
        if counted.get("terminating"):
            # A terminating result (ADR-0012 rule 6): no history, no skip events for the rest
            # of the turn, no further model call. ``drain_terminating`` holds the run while any
            # ledger entry is unresolved and lets an active stop decide the status.
            _log.warning("repair or denial budget exhausted", reason=counted["terminating"])
            return counted
        if result["outcome"] != CallOutcome.APPLIED and state["pending"]:
            ended = await record_skips(call, CallOutcome(result["outcome"]), state["pending"])
            if ended is not None:
                return ended
        history = [
            *state["history"],
            _tool_message(call["call_id"], result["outcome"], result["content"]),
        ]
        pending = state["pending"]
        if result["outcome"] != CallOutcome.APPLIED:
            # ADR-0012 rule 4: after a call that did not apply, the rest of the turn is skipped,
            # and each skipped result references that call's ID and outcome (identifiers only).
            skipped = {
                "reason": "earlier_call_not_applied",
                "trigger_call_id": call["call_id"],
                "trigger_outcome": CallOutcome(result["outcome"]).value,
            }
            history += [_tool_message(p["call_id"], CallOutcome.SKIPPED, skipped) for p in pending]
            pending = []
        return {
            **counted,
            "history": history,
            "pending": pending,
            "current": None,
            "decision": None,
            "ledger_id": None,
            "approval_id": None,
            "result": None,
        }

    def after_append(state: RunState) -> str:
        if state.get("divergence"):
            return "record_divergence"
        if state.get("terminating"):
            return "drain_terminating"
        return "finalize" if state.get("end_status") else "next_call"

    async def finalize(state: RunState) -> RunState:
        """TX11: the terminal status and its audit event, fenced."""
        status = RunStatus(state.get("end_status") or RunStatus.SUCCEEDED)
        summary = state.get("final_text")
        async with UnitOfWork(ctx.engine) as uow:
            runs = uow.repository(RunsRepository, ctx.scope)
            run = await runs.guard(ctx.run_id, ctx.generation)
            changed = await runs.transition(
                ctx.run_id,
                expected=NON_TERMINAL,
                new=status,
                generation=ctx.generation,
                outcome_summary=summary,
            )
            await uow.repository(AuditRepository, ctx.scope).append(
                ctx.keys,
                run_id=ctx.run_id,
                dedupe_key="run_finished",
                event_type="RUN_FINISHED",
                payload={"status": status.value, "summary": summary},
            )
            if changed:
                tracing.event("run.finished", **{"tether.run.status": status.value})
                duration = (datetime.now(UTC) - run["created_at"]).total_seconds()

                def finished() -> None:
                    metrics.run_finished(status.value, duration)
                    _log.info("run finished", status=status.value, duration_s=round(duration, 3))

                uow.on_commit(finished)
        return {}

    graph = StateGraph(RunState)
    graph.add_node("call_model", call_model)
    graph.add_node("next_call", next_call)
    graph.add_node("govern_call", govern_call)
    graph.add_node("request_approval", request_approval)
    graph.add_node("await_approval", await_approval)
    graph.add_node("authorize_call", authorize_call)
    graph.add_node("await_tool", await_tool)
    graph.add_node("append_result", append_result)
    graph.add_node("record_divergence", record_divergence)
    graph.add_node("drain_divergence", drain_divergence)
    graph.add_node("drain_terminating", drain_terminating)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges(
        "call_model", after_model, ["finalize", "next_call", "drain_divergence"]
    )
    graph.add_conditional_edges("next_call", after_next, ["govern_call", "call_model"])
    graph.add_conditional_edges(
        "govern_call",
        after_govern,
        ["finalize", "authorize_call", "request_approval", "append_result", "record_divergence"],
    )
    graph.add_conditional_edges(
        "request_approval",
        after_request,
        ["finalize", "await_approval", "append_result", "record_divergence"],
    )
    graph.add_conditional_edges(
        "await_approval",
        after_await_approval,
        ["append_result", "authorize_call", "record_divergence"],
    )
    graph.add_conditional_edges(
        "authorize_call",
        after_authorize,
        ["finalize", "append_result", "await_tool", "record_divergence"],
    )
    graph.add_conditional_edges(
        "await_tool",
        after_await_tool,
        ["finalize", "append_result", "record_divergence", "drain_terminating"],
    )
    graph.add_conditional_edges(
        "append_result",
        after_append,
        ["next_call", "finalize", "record_divergence", "drain_terminating"],
    )
    graph.add_edge("record_divergence", "drain_divergence")
    graph.add_edge("drain_divergence", "finalize")
    graph.add_edge("drain_terminating", "finalize")
    graph.add_edge("finalize", END)
    return graph
