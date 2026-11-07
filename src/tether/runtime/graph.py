"""The governed run graph (§9.2), compiled per invocation over a ``RunContext``.

Write nodes (``govern_call``, ``authorize_call``, ``finalize``) share-lock the run row, assert the
fencing token and check the stop predicate, and write their audit event in the same
transaction. Wait nodes (``await_tool``) only read the authoritative row and interrupt.
Decisions are keyed by step ordinal and adopted on replay, never re-decided.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.core.hashing import CallEnvelope, call_hash, call_hashes_equal, idempotency_key
from tether.core.ids import AgentId, RunId, WorkspaceId
from tether.core.results import CallOutcome
from tether.core.statuses import JobKind, RunStatus
from tether.policy.config import WorkspaceConfig
from tether.policy.engine import PolicyStaleError, evaluate_action
from tether.providers.history import ModelTurn, NeutralMessage
from tether.providers.protocol import ModelProvider, ProviderError
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


class RunState(TypedDict, total=False):
    history: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    current: dict[str, Any] | None
    decision: dict[str, Any] | None
    ledger_id: str | None
    approval_id: str | None
    result: dict[str, Any] | None
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
    approval_ttl_seconds: int


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
    """The keyed call hash (ADR-0010) under the run's pinned binding key. ``run`` is its row."""
    envelope = CallEnvelope(
        workspace_id=WorkspaceId(run["workspace_id"]),
        run_id=RunId(str(run["id"])),
        agent_id=AgentId(run["agent_id"]),
        tool=descriptor.name,
        tool_version=descriptor.version,
        arguments=args.model_dump(mode="json"),
        preconditions={},
    )
    key = keys.key(KeyFamily.BINDING, run["binding_key_id"]).secret.reveal()
    return call_hash(envelope, key=key, write=descriptor.effect == "write")


async def _assert_current_policy(uow: UnitOfWork, ctx: RunContext) -> None:
    active = await uow.repository(PolicyVersionsRepository, ctx.scope).active_version()
    if active != ctx.workspace.policy_version:
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


def build_graph(ctx: RunContext) -> StateGraph[RunState]:
    async def call_model(state: RunState) -> RunState:
        if state["turns"] >= ctx.max_turns:
            return {"end_status": RunStatus.FAILED, "final_text": "model turn budget exhausted"}
        history = [NeutralMessage.model_validate(m) for m in state["history"]]
        turn: ModelTurn | None = None
        failure = ""
        try:
            turn = await ctx.provider.complete(history, ctx.registry.manifest())
        except ProviderError as error:
            failure = f"model call failed: {error}"
        async with UnitOfWork(ctx.engine) as uow:
            await uow.repository(ModelCallsRepository, ctx.scope).add(
                {
                    "id": uuid.uuid4(),
                    "run_id": ctx.run_id,
                    "turn_ordinal": state["turns"],
                    "provider_role": "primary",
                    "provider": ctx.provider.name,
                    "model": ctx.provider.model,
                    "attempt": 1,
                    "occurrence_id": uuid.uuid4(),
                    "status": "failed" if turn is None else "succeeded",
                    "started_at": func.clock_timestamp(),
                    "finished_at": func.clock_timestamp(),
                }
            )
        if turn is None:
            return {"end_status": RunStatus.FAILED, "final_text": failure}
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
        }

    def after_next(state: RunState) -> str:
        return "govern_call" if state.get("current") else "call_model"

    async def govern_call(state: RunState) -> RunState:
        """TX2: adopt a persisted decision, or validate, evaluate policy and record it."""
        call = state["current"]
        call = _required(call, "current call")
        dedupe = f"decision:{call['step_ordinal']}"
        async with UnitOfWork(ctx.engine) as uow:
            run = await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
            audit = uow.repository(AuditRepository, ctx.scope)
            adopted = await audit.find(ctx.run_id, dedupe)
            if adopted is not None:
                return {"decision": adopted.payload}
            if run["stopped"]:
                return {"end_status": RunStatus.TIMED_OUT, "final_text": "deadline passed"}
            descriptor = ctx.registry.offered(call["tool"])
            args: BaseModel | list[str] = (
                _validate(descriptor, call["arguments"])
                if descriptor is not None
                else ["tool is not offered to this agent"]
            )
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
                return {"decision": payload}
            await _assert_current_policy(uow, ctx)
            decision = evaluate_action(
                ctx.workspace, tool=descriptor.name, requester=run["requester_snapshot"]
            )
            payload = {
                "decision": decision.outcome,
                "reasons": list(decision.reasons),
                "call_hash": call_hash_for(ctx.keys, run, descriptor, args),
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
            return {"decision": payload}

    def after_govern(state: RunState) -> str:
        if state.get("end_status"):
            return "finalize"
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
            existing = await approvals.by_ordinal(ctx.run_id, ordinal)
            if existing is not None:
                return {"approval_id": str(existing["id"])}
            run = await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
            if run["stopped"]:
                return {"end_status": RunStatus.TIMED_OUT, "final_text": "deadline passed"}
            descriptor = _required(ctx.registry.offered(call["tool"]), "offered tool")
            args = _validate(descriptor, call["arguments"])
            if isinstance(args, list):
                raise RuntimeError("a decided call no longer validates")
            approval_id = uuid.uuid4()
            masked = mask(args)
            await approvals.add(
                {
                    "id": approval_id,
                    "run_id": ctx.run_id,
                    "step_ordinal": ordinal,
                    "call_hash": decided["call_hash"],
                    "tool": descriptor.name,
                    "tool_version": descriptor.version,
                    "arguments_masked": masked,
                    "preconditions_masked": {},
                    "expires_at": func.clock_timestamp()
                    + func.make_interval(0, 0, 0, 0, 0, 0, ctx.approval_ttl_seconds),
                }
            )
            await uow.repository(AuditRepository, ctx.scope).append(
                ctx.keys,
                run_id=ctx.run_id,
                dedupe_key=f"approval_requested:{ordinal}",
                event_type="APPROVAL_REQUESTED",
                payload={
                    "approval_id": str(approval_id),
                    "call_hash": decided["call_hash"],
                    "arguments": masked,
                },
                tool=descriptor.name,
            )
        return {"approval_id": str(approval_id)}

    def after_request(state: RunState) -> str:
        return "finalize" if state.get("end_status") else "await_approval"

    async def await_approval(state: RunState) -> RunState:
        """Wait node: re-read the approval; interrupt while it is pending."""
        approval_id = uuid.UUID(_required(state["approval_id"], "approval id"))
        while True:
            async with UnitOfWork(ctx.engine) as uow:
                row = await uow.repository(ApprovalsRepository, ctx.scope).get(approval_id)
            approval = _required(row, "approval")
            if approval["status"] == "approved":
                return {}
            if approval["status"] == "rejected":
                return {"result": _result(CallOutcome.REJECTED, "rejected by approver")}
            if approval["status"] in ("expired", "closed"):
                return {"result": _result(CallOutcome.EXPIRED, f"approval {approval['status']}")}
            interrupt("awaiting_approval")

    def after_await_approval(state: RunState) -> str:
        return "append_result" if state.get("result") else "authorize_call"

    async def authorize_call(state: RunState) -> RunState:
        """TX5: ledger-first; else re-evaluate current policy and create the ledger entry."""
        call, decided = state["current"], state["decision"]
        call = _required(call, "current call")
        decided = _required(decided, "decision")
        ordinal = call["step_ordinal"]
        async with UnitOfWork(ctx.engine) as uow:
            ledger = uow.repository(LedgerRepository, ctx.scope)
            existing = await ledger.by_ordinal(ctx.run_id, ordinal)
            if existing is not None:
                return {"ledger_id": str(existing["id"])}
            run = await uow.repository(RunsRepository, ctx.scope).guard(ctx.run_id, ctx.generation)
            if run["stopped"]:
                return {"end_status": RunStatus.TIMED_OUT, "final_text": "deadline passed"}
            descriptor = ctx.registry.offered(call["tool"])
            args = _validate(descriptor, call["arguments"]) if descriptor else []
            if descriptor is None or isinstance(args, list):
                return {"result": _result(CallOutcome.NOT_APPLIED, "tool no longer resolvable")}
            await _assert_current_policy(uow, ctx)
            decision = evaluate_action(
                ctx.workspace, tool=descriptor.name, requester=run["requester_snapshot"]
            )
            hashed = call_hash_for(ctx.keys, run, descriptor, args)
            if not call_hashes_equal(hashed, decided["call_hash"]):
                return {"result": _result(CallOutcome.DENIED, "call changed since its decision")}
            if decision.outcome == "deny":
                return {"result": _result(CallOutcome.DENIED, "policy now: deny")}
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
            await uow.repository(AuditRepository, ctx.scope).append(
                ctx.keys,
                run_id=ctx.run_id,
                dedupe_key=f"authorized:{ordinal}",
                event_type="EXECUTION_AUTHORIZED",
                payload={"ledger_id": str(entry_id), "call_hash": hashed, "idempotency_key": key},
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
        if state.get("end_status"):
            return "finalize"
        return "append_result" if state.get("result") else "await_tool"

    async def await_tool(state: RunState) -> RunState:
        """Wait node: re-read the ledger entry; interrupt until it is terminal."""
        ledger_id = state["ledger_id"]
        ledger_id = _required(ledger_id, "ledger entry id")
        while True:
            async with UnitOfWork(ctx.engine) as uow:
                entry = await uow.repository(LedgerRepository, ctx.scope).get(uuid.UUID(ledger_id))
            entry = _required(entry, "ledger entry")
            if entry["state"] == "completed":
                return {"result": _result(CallOutcome.APPLIED, entry["outcome_payload_masked"])}
            if entry["state"] == "failed":
                return {"result": _result(CallOutcome.FAILED, entry["outcome_reason"])}
            waiting = entry["state"] == "indeterminate"
            interrupt("awaiting_reconciliation" if waiting else "awaiting_tool")

    async def append_result(state: RunState) -> RunState:
        call = state["current"]
        call = _required(call, "current call")
        result = state.get("result") or _result_from_decision(state["decision"])
        history = [
            *state["history"],
            _tool_message(call["call_id"], result["outcome"], result["content"]),
        ]
        pending = state["pending"]
        if result["outcome"] != CallOutcome.APPLIED:
            # ADR-0012: after a call that did not apply, the rest of the turn is skipped.
            history += [
                _tool_message(p["call_id"], CallOutcome.SKIPPED, "skipped") for p in pending
            ]
            pending = []
        return {
            "history": history,
            "pending": pending,
            "current": None,
            "decision": None,
            "ledger_id": None,
            "approval_id": None,
            "result": None,
        }

    async def finalize(state: RunState) -> RunState:
        """TX11: the terminal status and its audit event, fenced."""
        status = RunStatus(state.get("end_status") or RunStatus.SUCCEEDED)
        summary = state.get("final_text")
        async with UnitOfWork(ctx.engine) as uow:
            runs = uow.repository(RunsRepository, ctx.scope)
            await runs.guard(ctx.run_id, ctx.generation)
            await runs.transition(
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
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", after_model, ["finalize", "next_call"])
    graph.add_conditional_edges("next_call", after_next, ["govern_call", "call_model"])
    graph.add_conditional_edges(
        "govern_call",
        after_govern,
        ["finalize", "authorize_call", "request_approval", "append_result"],
    )
    graph.add_conditional_edges("request_approval", after_request, ["finalize", "await_approval"])
    graph.add_conditional_edges(
        "await_approval", after_await_approval, ["append_result", "authorize_call"]
    )
    graph.add_conditional_edges(
        "authorize_call", after_authorize, ["finalize", "append_result", "await_tool"]
    )
    graph.add_edge("await_tool", "append_result")
    graph.add_edge("append_result", "next_call")
    graph.add_edge("finalize", END)
    return graph
