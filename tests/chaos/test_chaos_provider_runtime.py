"""Provider retry (W3.3) and the repair/denial bound (W3.4) under crashes (bounded PD-24).

A worker dies in the middle of a provider retry sequence, or right after the run reached its
repair or denial bound. A fresh worker takes over; the run finishes exactly once, no proposal is
governed twice, every provider attempt that returned has its own ``model_calls`` row, and the
authoritative record stays complete (T-12) with a valid audit chain.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from tests.chaos.harness import TARGET, Crash, Env
from tests.chaos.test_chaos_crashes import drive_through_crashes

import tether.runtime.graph as graph_module
from tether.providers.history import ModelTurn, NeutralMessage
from tether.providers.protocol import ProviderUnavailableError
from tether.testing.scripted import ScriptedProvider

pytestmark = pytest.mark.chaos


class FlakyProvider(ScriptedProvider):
    """On model turn ``turn``, plays ``script`` (``unavailable``: a retryable failure; ``crash``:
    the process dies mid-attempt) once, in order, before answering normally."""

    def __init__(self, scenarios: Path, *, turn: int, script: Sequence[str]) -> None:
        super().__init__(scenarios, env="test")
        self.turn, self.script = turn, list(script)
        self.calls = 0

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        self.calls += 1
        index = sum(1 for message in history if message.role == "assistant")
        if index == self.turn and self.script:
            step = self.script.pop(0)
            if step == "crash":
                raise Crash(f"killed during a retry of model turn {index}")
            raise ProviderUnavailableError()
        return await super().complete(history, manifest)


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    async def immediately(seconds: float) -> None:
        return None

    monkeypatch.setattr(graph_module, "_sleep", immediately)


@pytest.mark.parametrize(
    "script",
    [["unavailable", "crash"], ["unavailable", "unavailable", "crash"]],
    ids=["crash-on-attempt-2", "crash-on-attempt-3"],
)
@pytest.mark.parametrize("turn", [0, 1])
@pytest.mark.asyncio
async def test_t10_a_crash_during_a_retry_sequence_resumes_without_governing_twice(
    env: Env, turn: int, script: list[str]
) -> None:
    provider = FlakyProvider(env.tmp_path, turn=turn, script=script)
    run_id, crashes, worker = await drive_through_crashes(
        env, None, scenario="write", provider=lambda: provider
    )
    try:
        assert crashes == 1
        assert env.status(run_id) == "succeeded"
        # The write was governed, authorized and sent exactly once.
        assert TARGET.sends == 1
        assert [(o, t, s) for o, t, s, _, _ in env.ledger(run_id)] == [
            (0, "probe_write", "completed")
        ]
        assert env.events(run_id).count("POLICY_DECISION") == 1
        # Every attempt that returned has its own row; the crashed attempt left none. After the
        # crash the node re-ran from attempt 1 (no deduplication of attempts).
        rows = env.sql(
            "SELECT turn_ordinal, attempt, status FROM tether.model_calls WHERE run_id = %s"
            " ORDER BY started_at",
            run_id,
        )
        failed_before_crash = len(script) - 1
        on_turn = [(a, s) for t, a, s in rows if t == turn]
        assert on_turn == [
            *[(n, "failed") for n in range(1, failed_before_crash + 1)],
            (1, "succeeded"),
        ]
        assert len(rows) == 2 + failed_before_crash
        await env.assert_reconstructable(run_id)
        await env.assert_terminal_stable(run_id, worker)
    finally:
        await worker.close()
