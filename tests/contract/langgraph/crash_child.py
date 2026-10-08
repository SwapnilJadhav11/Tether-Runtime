"""Child process for the crash-recovery contract tests.

Runs a two-node graph with synchronous durability and kills the process with ``os._exit``
inside the second node, so nothing after that point (no cleanup, no checkpoint) can run.
The tests import ``build_graph`` to resume the same graph in the parent process.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from tether.runtime.checkpointer import create_checkpointer, create_saver_pool

CRASH_EXIT_CODE = 137


class State(TypedDict):
    first: str
    second: str


def build_graph(checkpointer: Any, *, crash_in_second: bool, calls: list[str]) -> Any:
    def first(state: State) -> dict[str, str]:
        calls.append("first")
        return {"first": "done"}

    def second(state: State) -> dict[str, str]:
        calls.append("second")
        if crash_in_second:
            os._exit(CRASH_EXIT_CODE)
        return {"second": "done"}

    graph = StateGraph(State)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph.compile(checkpointer=checkpointer)


INITIAL_INPUT: State = {"first": "", "second": ""}


async def _run(dsn: str, thread_id: str) -> None:
    pool = create_saver_pool(dsn, name="contract-crash-child")
    await pool.open(wait=True, timeout=10)
    graph = build_graph(create_checkpointer(pool), crash_in_second=True, calls=[])
    await graph.ainvoke(
        INITIAL_INPUT, {"configurable": {"thread_id": thread_id}}, durability="sync"
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_run(os.environ["CRASH_CHILD_DSN"], os.environ["CRASH_CHILD_THREAD_ID"]))
    # Reaching this line means the graph finished without crashing: a test failure.
    sys.exit(3)
