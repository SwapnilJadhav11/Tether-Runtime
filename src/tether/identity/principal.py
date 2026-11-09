"""The authenticated principal (ADR-0013, AD-03): built only from validated credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Principal:
    """Who is calling: workspace, subject and roles from a validated token, and its audience.

    Roles are valid only within ``workspace_id``. The token itself is never kept.
    """

    workspace_id: str
    subject: str
    roles: tuple[str, ...]
    audience: str
    token_id: str | None = None

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def requester(self) -> dict[str, Any]:
        """The snapshot stored as a run's requester identity (``runs.requester_snapshot``)."""
        return {"sub": self.subject, "roles": list(self.roles)}
