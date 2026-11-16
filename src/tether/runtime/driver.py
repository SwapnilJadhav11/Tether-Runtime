"""Run driver: one ``advance_run`` invocation of the run graph (§9.3, ADR-0005, ADR-0006).

The caller holds the run's advisory lock and has claimed the fencing token. The driver picks the
invocation (fresh start, resume a pending interrupt with the sentinel, or continue after a
crash), runs the graph with ``durability="sync"``, and records the waiting status on interrupt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.core.statuses import RunStatus
from tether.observability import metrics, tracing
from tether.observability.logging import get_logger
from tether.policy.config import BootstrapFile, load_bootstrap
from tether.policy.engine import (
    BuiltinPolicyEngine,
    ToolPolicyMetadata,
    check_tools_against_policy,
)
from tether.providers.factory import build_provider
from tether.providers.protocol import ModelProvider
from tether.registry.registry import ToolRegistry, load_workload
from tether.runtime.checkpointer import SaverPool, create_checkpointer, create_saver_pool
from tether.runtime.graph import (
    RunContext,
    build_graph,
    initial_state,
    reconciled_outcomes,
    stop_ending,
)
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyRing
from tether.settings import Settings
from tether.storage.engine import create_database_engine
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.ledger import LedgerRepository
from tether.storage.repos.runs import NON_TERMINAL, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

# Opaque, meaningless resume value (§9.3; pinned by the S1.3 contract suite).
WAKE = "tether:wake"
# The typed terminal reason of a run whose agent or workspace this worker does not have (I-4).
CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
_log = get_logger(__name__)

_WAIT_STATUS = {
    "awaiting_tool": RunStatus.AWAITING_TOOL,
    "awaiting_approval": RunStatus.AWAITING_APPROVAL,
    "awaiting_reconciliation": RunStatus.AWAITING_RECONCILIATION,
}


@dataclass
class WorkerRuntime:
    settings: Settings
    engine: AsyncEngine
    saver_pool: SaverPool
    checkpointer: AsyncPostgresSaver
    keys: KeyRing
    bootstrap: BootstrapFile
    provider: ModelProvider
    registries: dict[str, ToolRegistry] = field(default_factory=dict)

    @classmethod
    async def open(
        cls,
        settings: Settings,
        *,
        bootstrap: BootstrapFile | None = None,
        provider: ModelProvider | None = None,
    ) -> WorkerRuntime:
        if bootstrap is None:
            bootstrap = (
                load_bootstrap(settings.bootstrap_file)
                if settings.bootstrap_file
                else BootstrapFile()
            )
        # Fail closed at startup if a configured workload is missing or broken.
        registries = {
            agent.workload: ToolRegistry(load_workload(agent.workload))
            for ws in bootstrap.workspaces
            for agent in ws.agents
        }
        # Fail closed at startup if a policy allows a high-risk tool (ADR-0020 rule 4).
        for ws in bootstrap.workspaces:
            tools = {
                tool.name: ToolPolicyMetadata.from_descriptor(tool)
                for agent in ws.agents
                for tool in registries[agent.workload].workload.tools
            }
            check_tools_against_policy(ws, tools)
        dsn = settings.database_url.get_secret_value()
        pool = create_saver_pool(dsn, name="tether-worker-langgraph")
        await pool.open(wait=False)
        return cls(
            settings=settings,
            engine=create_database_engine(dsn),
            saver_pool=pool,
            checkpointer=create_checkpointer(pool),
            keys=KeyRing(EnvSecretsProvider()),
            bootstrap=bootstrap,
            # Configuration chooses the provider (ADR-0016 rule 6). Tests may inject one.
            provider=provider if provider is not None else build_provider(settings),
            registries=registries,
        )

    async def close(self) -> None:
        # A hosted adapter holds a connection pool. The protocol does not require `aclose`,
        # so a provider that has none (the scripted one) is simply left alone.
        closer = getattr(self.provider, "aclose", None)
        if closer is not None:
            await closer()
        await self.engine.dispose()
        await self.saver_pool.close()


async def _configuration_unavailable(
    runtime: WorkerRuntime, scope: WorkspaceScope, run_id: uuid.UUID, gen: int
) -> None:
    """The run's agent or workspace is not configured on this worker (scope delta I-4).

    Nothing is decided, authorized or sent: the graph is not invoked. While any ledger entry of
    the run is unresolved, the run is held for reconciliation (ADR-0018) and its progress is left
    untouched, so the configuration failure never hides a real-world effect; that entry's own
    outcome or its reconciliation wakes the run. Once every entry is resolved, an active stop
    decides (ADR-0006), with the reason in the summary; otherwise the run ends ``failed`` with the
    typed reason and any reconciled outcome.
    """
    async with UnitOfWork(runtime.engine) as uow:
        runs = uow.repository(RunsRepository, scope)
        run = await runs.guard(run_id, gen)
        if await uow.repository(LedgerRepository, scope).unresolved(run_id):
            held = RunStatus.AWAITING_RECONCILIATION
            await runs.transition(run_id, expected=RunStatus.RUNNING, new=held, generation=gen)
            uow.on_commit(lambda: _log.warning("run held: configuration unavailable"))
            return
        if run["stopped"]:
            status, ended = await stop_ending(uow, scope, run)
            summary = f"{ended}; {CONFIGURATION_UNAVAILABLE}"
        else:
            status = RunStatus.FAILED
            reconciled = await reconciled_outcomes(uow, scope, run_id)
            summary = "; ".join([CONFIGURATION_UNAVAILABLE, *reconciled])
        await runs.transition(
            run_id, expected=NON_TERMINAL, new=status, generation=gen, outcome_summary=summary
        )
        await uow.repository(AuditRepository, scope).append(
            runtime.keys,
            run_id=run_id,
            dedupe_key="run_finished",
            event_type="RUN_FINISHED",
            payload={"status": status.value, "summary": summary},
        )

        def finished() -> None:
            metrics.run_finished(status.value, None)
            _log.warning("run ended: configuration unavailable", status=status.value)

        uow.on_commit(finished)


async def drive(
    runtime: WorkerRuntime, scope: WorkspaceScope, run_id: uuid.UUID, generation: int
) -> None:
    async with UnitOfWork(runtime.engine) as uow:
        run = await uow.repository(RunsRepository, scope).get(run_id)
    if run is None:
        raise RuntimeError(f"run {run_id} vanished from its workspace")
    workspace = runtime.bootstrap.workspace(scope.workspace_id)
    agent = workspace.agent(run["agent_id"]) if workspace else None
    if workspace is None or agent is None:
        await _configuration_unavailable(runtime, scope, run_id, generation)
        return
    registry = runtime.registries[agent.workload]
    ctx = RunContext(
        engine=runtime.engine,
        keys=runtime.keys,
        workspace=workspace,
        registry=registry,
        provider=runtime.provider,
        scope=scope,
        run_id=run_id,
        agent_id=agent.id,
        generation=generation,
        max_turns=runtime.settings.max_model_turns,
        approval_ttl_seconds=runtime.settings.approval_ttl_seconds,
        policy=BuiltinPolicyEngine(workspace),
        provider_max_attempts=runtime.settings.provider_max_attempts,
        max_repairs=runtime.settings.max_repairs,
        max_denials=runtime.settings.max_denials,
    )
    graph = build_graph(ctx).compile(checkpointer=runtime.checkpointer)
    config: Any = {"configurable": {"thread_id": str(run_id)}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        invocation: Any = initial_state(registry.workload.agent.system_prompt, run["task_input"])
        mode = "start"
    elif snapshot.interrupts:
        invocation = Command(resume=WAKE)
        mode = "resume"
    elif snapshot.next:
        invocation = None  # crashed mid-step: continue from the last checkpoint
        mode = "recover"
    else:
        return  # the graph already finished; finalize recorded the terminal status
    with tracing.span(
        "run.drive",
        attributes={
            "tether.run_id": run_id,
            "tether.driver.generation": generation,
            "tether.driver.mode": mode,
        },
    ) as current:
        await graph.ainvoke(invocation, config, durability="sync")
        snapshot = await graph.aget_state(config)
        if snapshot.interrupts:
            waiting = _WAIT_STATUS[snapshot.interrupts[0].value]
            tracing.annotate(current, **{"tether.run.waiting": waiting.value})
            async with UnitOfWork(runtime.engine) as uow:
                runs = uow.repository(RunsRepository, scope)
                await runs.guard(run_id, generation)
                await runs.transition(
                    run_id, expected=RunStatus.RUNNING, new=waiting, generation=generation
                )
            _log.info("run waiting", status=waiting.value)
