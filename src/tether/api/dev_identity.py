"""TEMPORARY dev/test-only identity shortcut. S1.8 replaces this whole module.

ADR-0013 requires the workspace, agent and requester to come from a validated tasks JWT plus an
API key. Until S1.8 exists, the walking skeleton takes them from the request instead: a
``dev_identity`` body object on submit and a ``dev_workspace_id`` query parameter on reads.

Rules that keep the shortcut contained:
- Every route that uses it is refused with ``404`` when ``TETHER_ENV`` is ``prod``.
- It only ever produces a ``Principal`` or a ``WorkspaceScope``. Handlers, storage and the worker
  never see where identity came from, so S1.8 can swap these dependencies without other changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Body, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from tether.core.ids import WorkspaceId
from tether.storage.scope import WorkspaceScope


@dataclass(frozen=True)
class Principal:
    """Who is submitting: resolved identity, never taken from task input."""

    workspace_id: str
    agent_id: str
    requester: dict[str, Any]


class DevRequester(BaseModel):
    sub: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)


class DevIdentity(BaseModel):
    workspace_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    requester: DevRequester


def refuse_in_prod(request: Request) -> None:
    if request.app.state.settings.env == "prod":
        raise HTTPException(status.HTTP_404_NOT_FOUND)


def dev_submit_principal(dev_identity: Annotated[DevIdentity, Body()]) -> Principal:
    return Principal(
        workspace_id=dev_identity.workspace_id,
        agent_id=dev_identity.agent_id,
        requester=dev_identity.requester.model_dump(),
    )


def dev_read_scope(dev_workspace_id: Annotated[str, Query(min_length=1)]) -> WorkspaceScope:
    return WorkspaceScope(WorkspaceId(dev_workspace_id))
