"""Contract (a): with durability="sync", a node sees the previous step's checkpoint committed.

ADR-0003 relies on this: call_model's checkpoint (step ordinals, proposals) must be durable
before any downstream node creates an authoritative row. The check reads the checkpoint
through an independent connection, not the graph's own saver.
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg import Connection

from tether.runtime.checkpointer import SAVER_CONNECTION_KWARGS

NOT_COMMITTED = "<no checkpoint>"


class State(TypedDict):
    marker: str
    seen: str


def _committed_marker(dsn: str, config: dict[str, Any]) -> str:
    """Read the latest committed checkpoint for the thread on a separate connection."""
    with Connection.connect(dsn, **SAVER_CONNECTION_KWARGS) as conn:
        saved = PostgresSaver(conn).get_tuple(config)
    if saved is None:
        return NOT_COMMITTED
    return str(saved.checkpoint["channel_values"].get("marker", NOT_COMMITTED))


def _graph(checkpointer: Any, dsn: str, config: dict[str, Any]) -> Any:
    def first(state: State) -> dict[str, str]:
        return {"marker": "written-by-first"}

    def second(state: State) -> dict[str, str]:
        return {"seen": _committed_marker(dsn, config)}

    graph = StateGraph(State)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    return graph.compile(checkpointer=checkpointer)


@pytest.mark.asyncio
async def test_sync_durability_commits_previous_step_before_next_node(
    checkpointer: Any, migrated_db: Any, thread_config: dict[str, Any]
) -> None:
    graph = _graph(checkpointer, migrated_db.app_dsn, thread_config)
    result = await graph.ainvoke({"marker": "", "seen": ""}, thread_config, durability="sync")
    assert result["seen"] == "written-by-first"


@pytest.mark.asyncio
async def test_exit_durability_does_not_commit_before_next_node(
    checkpointer: Any, migrated_db: Any, thread_config: dict[str, Any]
) -> None:
    """Control: the same probe sees nothing under durability="exit", so it can fail."""
    graph = _graph(checkpointer, migrated_db.app_dsn, thread_config)
    result = await graph.ainvoke({"marker": "", "seen": ""}, thread_config, durability="exit")
    assert result["seen"] == NOT_COMMITTED
