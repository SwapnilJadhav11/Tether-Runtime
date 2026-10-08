"""Contract (d): after a process dies mid-node, ``invoke(None)`` re-runs that node.

ADR-0003/§9.3 step 3: a checkpoint with a non-empty ``next`` and no interrupt means a crash
mid-step, and the driver continues with ``invoke(None)`` on the same thread. The node that was
running re-executes from the last checkpoint; completed nodes do not run again.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_crash_mid_node_then_invoke_none_reruns_that_node(
    checkpointer: Any, crash_child: ModuleType, crashed_thread: dict[str, Any]
) -> None:
    calls: list[str] = []
    graph = crash_child.build_graph(checkpointer, crash_in_second=False, calls=calls)

    snapshot = await graph.aget_state(crashed_thread)
    assert snapshot.values["first"] == "done", "the first node's checkpoint survived the crash"
    assert snapshot.next == ("second",)
    assert not snapshot.interrupts

    result = await graph.ainvoke(None, crashed_thread, durability="sync")

    assert calls == ["second"], "only the interrupted node re-runs; completed nodes do not"
    assert result == {"first": "done", "second": "done"}
