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
from tether.policy.config import BootstrapFile, load_bootstrap
from tether.providers.protocol import ModelProvider
from tether.registry.registry import ToolRegistry, load_workload
from tether.runtime.checkpointer import SaverPool, create_checkpointer, create_saver_pool
from tether.runtime.graph import RunContext, build_graph, initial_state
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyRing
from tether.settings import Settings
from tether.storage.engine import create_database_engine
from tether.storage.repos.audit import AuditRepository
from tether.storage.repos.runs import NON_TERMINAL, RunsRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork
from tether.testing.scripted import ScriptedProvider

# Opaque, meaningless resume value (§9.3; pinned by the S1.3 contract suite).
WAKE = "tether:wake"

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
            provider=provider or ScriptedProvider(settings.scenarios_dir, env=settings.env),
            registries=registries,
        )

    async def close(self) -> None:
        await self.engine.dispose()
        await self.saver_pool.close()


async def _fail(runtime: WorkerRuntime, scope: WorkspaceScope, run_id: uuid.UUID, gen: int) -> None:
    """The run's agent is not configured on this worker: fail the run, audited."""
    async with UnitOfWork(runtime.engine) as uow:
        await uow.repository(RunsRepository, scope).transition(
            run_id,
            expected=NON_TERMINAL,
            new=RunStatus.FAILED,
            generation=gen,
            outcome_summary="agent is not configured",
        )
        await uow.repository(AuditRepository, scope).append(
            runtime.keys,
            run_id=run_id,
            dedupe_key="run_finished",
            event_type="RUN_FINISHED",
            payload={"status": "failed", "summary": "agent is not configured"},
        )


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
        await _fail(runtime, scope, run_id, generation)
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
    )
    graph = build_graph(ctx).compile(checkpointer=runtime.checkpointer)
    config: Any = {"configurable": {"thread_id": str(run_id)}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        invocation: Any = initial_state(registry.workload.agent.system_prompt, run["task_input"])
    elif snapshot.interrupts:
        invocation = Command(resume=WAKE)
    elif snapshot.next:
        invocation = None  # crashed mid-step: continue from the last checkpoint
    else:
        return  # the graph already finished; finalize recorded the terminal status
    await graph.ainvoke(invocation, config, durability="sync")
    snapshot = await graph.aget_state(config)
    if snapshot.interrupts:
        waiting = _WAIT_STATUS[snapshot.interrupts[0].value]
        async with UnitOfWork(runtime.engine) as uow:
            runs = uow.repository(RunsRepository, scope)
            await runs.guard(run_id, generation)
            await runs.transition(
                run_id, expected=RunStatus.RUNNING, new=waiting, generation=generation
            )
