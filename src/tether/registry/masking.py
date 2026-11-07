"""The single masking choke point (ADR-0017).

Every tool output and every argument that is persisted, logged, audited or shown goes through
``mask``: fields annotated ``Sensitive`` (``tether.sdk``) are replaced, recursively.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tether.sdk import Sensitive

MASK = "**********"


def _is_sensitive(metadata: list[Any]) -> bool:
    return any(item is Sensitive or isinstance(item, Sensitive) for item in metadata)


def _masked(value: Any, dumped: Any) -> Any:
    """``dumped`` is ``value`` in JSON mode; mask any models nested inside it."""
    if isinstance(value, BaseModel):
        return mask(value)
    if isinstance(value, list | tuple):
        return [_masked(item, out) for item, out in zip(value, dumped, strict=True)]
    if isinstance(value, dict):
        return {key: _masked(value[key], dumped[key]) for key in value}
    return dumped


def mask(model: BaseModel) -> dict[str, Any]:
    """JSON-ready dict of ``model`` with every ``Sensitive`` field replaced by ``MASK``."""
    dumped = model.model_dump(mode="json")
    for name, info in type(model).model_fields.items():
        dumped[name] = (
            MASK if _is_sensitive(info.metadata) else _masked(getattr(model, name), dumped[name])
        )
    return dumped
