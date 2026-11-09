"""Dev/test-only identity shortcut, isolated from the real trust boundary (ADR-0013).

Real requests carry a JWT (and, on tasks routes, the workspace API key); ``tether.authz.deps``
validates them. Only when a request carries **no** ``Authorization`` header and ``TETHER_ENV`` is
``dev`` or ``test`` may it instead state its identity: a ``dev_identity`` body object (submit,
approval decisions, reconciliations, cancellations) or a ``dev_workspace_id`` query parameter
(reads). In ``prod`` the shortcut is never accepted: such a request has no credentials → ``401``.

The shortcut only produces the same ``Principal`` that real credentials produce, so every
authorization rule after authentication is identical for both.
"""

from __future__ import annotations

from fastapi import Request
from pydantic import BaseModel, Field

from tether.identity.principal import Principal

DEV_ENVS = frozenset({"dev", "test"})


class DevRequester(BaseModel):
    sub: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)


class DevIdentity(BaseModel):
    """Submit: the workspace, agent and requester, stated by the caller (dev/test only)."""

    workspace_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    requester: DevRequester


class DevPerson(BaseModel):
    """Any other human action: workspace, subject and roles, stated by the caller."""

    workspace_id: str = Field(min_length=1)
    sub: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)


def allowed(request: Request) -> bool:
    return bool(request.app.state.settings.env in DEV_ENVS)


def submitter(dev: DevIdentity, audience: str) -> Principal:
    return Principal(
        workspace_id=dev.workspace_id,
        subject=dev.requester.sub,
        roles=tuple(dev.requester.roles),
        audience=audience,
    )


def person(dev: DevPerson, audience: str) -> Principal:
    return Principal(
        workspace_id=dev.workspace_id, subject=dev.sub, roles=tuple(dev.roles), audience=audience
    )
