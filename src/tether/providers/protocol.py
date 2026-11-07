"""The model-provider protocol (AD-06). One implementation for the MVP: the scripted provider."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from tether.providers.history import ModelTurn, NeutralMessage


class ProviderError(Exception):
    """The model call failed; the run records a failed model call."""


class ModelProvider(Protocol):
    name: str
    model: str

    async def complete(
        self, history: Sequence[NeutralMessage], manifest: Sequence[dict[str, Any]]
    ) -> ModelTurn: ...
