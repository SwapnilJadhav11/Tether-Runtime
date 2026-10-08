"""Contracts (b) and (c): interrupt/resume semantics that wait nodes rely on (ADR-0003, §9.3).

(b) Resuming re-executes the interrupted node from its first line.
(c) A resume value carries no meaning. The driver resumes with the opaque sentinel ``WAKE``
    (architecture §9.3), because on the pinned LangGraph ``Command(resume=None)`` is not a
    valid resume (see the README). Both facts are pinned here.

Wait nodes check their authoritative condition before interrupting and loop on spurious
wakes, so a resume while the condition is still unsatisfied must pause the run again.
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

# The constant, meaningless resume value the run driver uses (README: "Resume value").
WAKE = "tether:wake"


class State(TypedDict):
    done: bool


class WaitProbe:
    """A wait node: count entries, and interrupt while the external condition is unsatisfied."""

    def __init__(self) -> None:
        self.ready = False
        self.entries = 0
        self.resume_values: list[Any] = []

    def node(self, state: State) -> dict[str, bool]:
        self.entries += 1
        while not self.ready:
            self.resume_values.append(interrupt("awaiting"))
        return {"done": True}


def _graph(checkpointer: Any, probe: WaitProbe) -> Any:
    graph = StateGraph(State)
    graph.add_node("wait", probe.node)
    graph.add_edge(START, "wait")
    graph.add_edge("wait", END)
    return graph.compile(checkpointer=checkpointer)


async def _start_paused(graph: Any, config: dict[str, Any], probe: WaitProbe) -> None:
    result = await graph.ainvoke({"done": False}, config, durability="sync")
    assert "__interrupt__" in result
    assert probe.entries == 1
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("wait",)
    assert snapshot.interrupts


@pytest.mark.asyncio
async def test_resume_reexecutes_node_from_first_line(
    checkpointer: Any, thread_config: dict[str, Any]
) -> None:
    probe = WaitProbe()
    graph = _graph(checkpointer, probe)
    await _start_paused(graph, thread_config, probe)

    probe.ready = True
    result = await graph.ainvoke(Command(resume=WAKE), thread_config, durability="sync")

    assert result == {"done": True}
    assert probe.entries == 2, "the node must run again from its first line on resume"


@pytest.mark.asyncio
async def test_constant_sentinel_resume_is_accepted_and_ignored(
    checkpointer: Any, thread_config: dict[str, Any]
) -> None:
    probe = WaitProbe()
    graph = _graph(checkpointer, probe)
    await _start_paused(graph, thread_config, probe)

    probe.ready = True
    result = await graph.ainvoke(Command(resume=WAKE), thread_config, durability="sync")

    assert result == {"done": True}
    assert probe.entries == 2
    snapshot = await graph.aget_state(thread_config)
    assert snapshot.next == ()
    assert not snapshot.interrupts


@pytest.mark.asyncio
async def test_resume_with_none_is_not_a_valid_resume_on_pinned_version(
    checkpointer: Any, thread_config: dict[str, Any]
) -> None:
    """Why the sentinel exists. If a LangGraph bump makes this fail, re-evaluate the README."""
    probe = WaitProbe()
    graph = _graph(checkpointer, probe)
    await _start_paused(graph, thread_config, probe)

    probe.ready = True
    with pytest.raises(UnboundLocalError):
        await graph.ainvoke(Command(resume=None), thread_config, durability="sync")


@pytest.mark.asyncio
async def test_spurious_wake_loops_and_pauses_again(
    checkpointer: Any, thread_config: dict[str, Any]
) -> None:
    probe = WaitProbe()
    graph = _graph(checkpointer, probe)
    await _start_paused(graph, thread_config, probe)

    # Woken while the condition is still unsatisfied: the node re-runs, consumes the
    # meaningless resume value, loops, and interrupts again.
    result = await graph.ainvoke(Command(resume=WAKE), thread_config, durability="sync")
    assert "__interrupt__" in result
    assert probe.entries == 2
    snapshot = await graph.aget_state(thread_config)
    assert snapshot.next == ("wait",)
    assert snapshot.interrupts

    probe.ready = True
    result = await graph.ainvoke(Command(resume=WAKE), thread_config, durability="sync")
    assert result == {"done": True}
    assert probe.entries == 3
