"""Contract (e): state inspection distinguishes the four §9.3 step-3 branches.

The run driver (S2.8) chooses how to invoke a run from its LangGraph state:

    no checkpoint                         -> invoke with the initial input
    pending interrupt(s)                  -> resume (Command)
    non-empty ``next``, no interrupt      -> invoke(None)   (crash mid-step)
    otherwise (finished)                  -> TX11 finalize

``invocation_choice`` below is the reference mapping this suite pins; the README records it.
Interrupts are checked before ``next`` because a paused thread also has a non-empty ``next``.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Literal, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

Choice = Literal["start", "resume", "continue", "finalize"]


def invocation_choice(snapshot: Any) -> Choice:
    if snapshot.created_at is None:
        return "start"
    if snapshot.interrupts:
        return "resume"
    if snapshot.next:
        return "continue"
    return "finalize"


class State(TypedDict):
    done: bool


def _graph(checkpointer: Any, *, pause: bool) -> Any:
    def step(state: State) -> dict[str, bool]:
        if pause:
            interrupt("awaiting")
        return {"done": True}

    graph = StateGraph(State)
    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph.compile(checkpointer=checkpointer)


@pytest.mark.asyncio
async def test_no_checkpoint(checkpointer: Any, thread_config: dict[str, Any]) -> None:
    snapshot = await _graph(checkpointer, pause=False).aget_state(thread_config)
    assert invocation_choice(snapshot) == "start"


@pytest.mark.asyncio
async def test_pending_interrupt(checkpointer: Any, thread_config: dict[str, Any]) -> None:
    graph = _graph(checkpointer, pause=True)
    await graph.ainvoke({"done": False}, thread_config, durability="sync")
    snapshot = await graph.aget_state(thread_config)
    assert snapshot.next, "a paused thread also has a non-empty next"
    assert invocation_choice(snapshot) == "resume"


@pytest.mark.asyncio
async def test_next_without_interrupt_after_crash(
    checkpointer: Any, crash_child: ModuleType, crashed_thread: dict[str, Any]
) -> None:
    graph = crash_child.build_graph(checkpointer, crash_in_second=False, calls=[])
    snapshot = await graph.aget_state(crashed_thread)
    assert invocation_choice(snapshot) == "continue"


@pytest.mark.asyncio
async def test_finished(checkpointer: Any, thread_config: dict[str, Any]) -> None:
    graph = _graph(checkpointer, pause=False)
    await graph.ainvoke({"done": False}, thread_config, durability="sync")
    snapshot = await graph.aget_state(thread_config)
    assert invocation_choice(snapshot) == "finalize"
