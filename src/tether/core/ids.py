"""Identifiers that appear in call envelopes.

``run_id`` is a UUID in canonical lowercase hyphenated form. Workspace and agent IDs are the
string identifiers declared in bootstrap configuration.
"""

from __future__ import annotations

import uuid
from typing import NewType

from tether.core.errors import InvalidCallError

RunId = NewType("RunId", str)
WorkspaceId = NewType("WorkspaceId", str)
AgentId = NewType("AgentId", str)


def parse_run_id(value: str | uuid.UUID) -> RunId:
    """Return the canonical lowercase form of a run ID."""
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise InvalidCallError("run_id is not a valid UUID") from None
    return RunId(str(parsed))


def is_canonical_run_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False
