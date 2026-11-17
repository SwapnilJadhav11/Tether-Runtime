"""W5.2 ledger edge cases under crashes: an unresolved effect is never hidden.

- Configuration unavailable (scope delta I-4): after a deployment that removed the run's agent,
  the process dies right after a critical transaction commits; fresh workers (still without the
  agent) take over. The dispatched write gets an honest unknown outcome and is never re-sent, the
  run is held until a human reconciles it, and only then ends ``failed`` with the typed reason,
  exactly once.
- Pinned version gone after the ledger row exists (ADR-0011 rule 5): the process dies right after
  TX5 or TX6 commits and the restarted worker no longer has the pinned version. The replay adopts
  the ledger row (no ``REPLAY_DIVERGENCE``), nothing is sent, and the outcome follows from what is
  known: never dispatched is not applied; dispatched is indeterminate.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
from tests.chaos.harness import (
    BOOT,
    TARGET,
    TERMINAL,
    TEST_WORKLOAD,
    Env,
    crash_after_commit,
    is_crash,
)
from workloads.incident_remediation import WORKLOAD

from tether.policy.config import BootstrapFile
from tether.registry.registry import ToolRegistry
from tether.runtime.driver import WorkerRuntime
from tether.sdk import AgentDefinition, Workload
from tether.worker.main import process_next_job

pytestmark = pytest.mark.chaos

# The same workspace after a deployment that removed its agent.
WITHOUT_AGENT = BootstrapFile.model_validate(
    {
        "workspaces": [
            {
                **BOOT.workspaces[0].model_dump(mode="json"),
                "agents": [],
                "policy": {
                    **BOOT.workspaces[0].policy.model_dump(mode="json"),
                    "agent_permissions": {},
                },
            }
        ]
    }
)
# A deployment of the workload without (offered or retained) ``probe_write``.
WITHOUT_PROBE_WRITE = Workload(
    name=TEST_WORKLOAD.name,
    tools=tuple(t for t in TEST_WORKLOAD.tools if t.name != "probe_write"),
    agent=AgentDefinition(
        system_prompt="chaos",
        tools=tuple(name for name in TEST_WORKLOAD.agent.tools if name != "probe_write"),
    ),
)

Factory = Callable[[], Awaitable[WorkerRuntime]]


async def settle(env: Env, worker: WorkerRuntime, new_worker: Factory) -> tuple[int, WorkerRuntime]:
    """Drain, replacing the worker after every crash, until nothing is due."""
    crashes = 0
    for _ in range(30):
        try:
            await env.drain(worker)
        except BaseException as error:
            if not is_crash(error):
                raise
            crashes += 1
            await worker.close()
            env.expire_leases()
            worker = await new_worker()
            continue
        return crashes, worker
    raise AssertionError("the queue did not settle")


def entry_row(env: Env, run_id: uuid.UUID) -> tuple[object, ...]:
    """(id, state, outcome_class, attempts, resolution) of the run's only entry."""
    [found] = env.sql(
        "SELECT id, state, outcome_class, attempts, resolution FROM tether.ledger_entries"
        " WHERE run_id = %s",
        run_id,
    )
    return found


@pytest.mark.parametrize("event", ["TOOL_OUTCOME", "RUN_FINISHED"])
@pytest.mark.asyncio
async def test_i4_configuration_loss_holds_a_dispatched_write_across_crashes(
    env: Env, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    TARGET.script[:] = ["timeout"]
    run_id = await env.submit("write")
    configured = await env.worker()
    try:
        while await process_next_job(configured, ["advance_run"]):
            pass
        # The send times out; its re-send is due when a deployment removes the agent.
        assert await process_next_job(configured, ["execute_tool"])
    finally:
        await configured.close()
    assert entry_row(env, run_id)[1] == "dispatched"

    async def unconfigured() -> WorkerRuntime:
        worker = await env.worker()
        worker.bootstrap = WITHOUT_AGENT
        return worker

    crash_after_commit(monkeypatch, event)
    crashes, worker = await settle(env, await unconfigured(), unconfigured)
    try:
        entry_id, state, outcome_class, attempts, _ = entry_row(env, run_id)
        assert (state, outcome_class) == ("indeterminate", "unknown")
        assert TARGET.sends == attempts == 1  # never re-sent without the configuration
        assert env.status(run_id) == "awaiting_reconciliation"
        assert "RUN_FINISHED" not in env.events(run_id)
        await env.assert_reconstructable(run_id)
        assert (await env.reconcile(entry_id, "applied")).status_code == 200
        more, worker = await settle(env, worker, unconfigured)
        assert crashes + more == 1
        assert env.status(run_id) == "failed"
        [(summary,)] = env.sql("SELECT outcome_summary FROM tether.runs WHERE id = %s", run_id)
        assert summary == "configuration_unavailable; probe_write reconciled_applied"
        assert env.events(run_id).count("RUN_FINISHED") == 1
        assert env.model_calls(run_id) == 1
        assert TARGET.sends == 1
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()


@pytest.mark.parametrize(
    ("event", "state", "outcome_class", "status"),
    [
        ("EXECUTION_AUTHORIZED", "failed", "not_applied", "succeeded"),
        ("DISPATCHED", "indeterminate", "unknown", "awaiting_reconciliation"),
    ],
)
@pytest.mark.asyncio
async def test_adr0011_a_version_gone_after_a_crash_is_followed_never_diverged(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    state: str,
    outcome_class: str,
    status: str,
) -> None:
    async def redeployed() -> WorkerRuntime:
        worker = await env.worker()
        worker.registries[WORKLOAD.name] = ToolRegistry(WITHOUT_PROBE_WRITE)
        return worker

    crash_after_commit(monkeypatch, event)
    run_id = await env.submit("write")
    first = await env.worker()
    crashes, worker = await settle(env, first, redeployed)
    try:
        assert crashes == 1
        _, found_state, found_class, _, _ = entry_row(env, run_id)
        assert (found_state, found_class) == (state, outcome_class)
        assert TARGET.sends == 0  # the crash came before any send; nothing is sent after it
        assert env.status(run_id) == status
        assert "REPLAY_DIVERGENCE" not in env.events(run_id)
        await env.assert_reconstructable(run_id)
        if status in TERMINAL:
            await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
