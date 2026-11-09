"""Route-bound authentication (ADR-0013, AD-03): one dependency per audience.

The expected audience is fixed by the route that uses the dependency, never read from the token:
``/v1/tasks/*`` → ``tether-tasks`` (plus the workspace API key), ``/v1/approvals/*`` →
``tether-approvals``, ``/v1/ops/*`` → ``tether-ops``. A request with an ``Authorization`` header
is authenticated with real credentials only; anything it states in its body is ignored. Without
one, only the dev/test shortcut can identify the caller (``tether.api.dev_identity``). Every
failure is ``401``, raised before the handler runs, so nothing is evaluated or written. Credentials
are never logged or echoed.

Authorization (role, ownership, policy eligibility) happens afterwards, in the handlers, with the
resulting ``Principal``; out-of-scope or unauthorized resources are ``404`` there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Body, HTTPException, Query, Request, status

from tether.api import dev_identity
from tether.api.dev_identity import DevIdentity, DevPerson
from tether.core.ids import WorkspaceId
from tether.identity import api_keys
from tether.identity.jwt import Audience, InvalidCredentials, database_issuers, validate_token
from tether.identity.principal import Principal
from tether.storage.scope import WorkspaceScope


def unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "invalid or missing credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization")
    if value is None:
        return None
    scheme, _, credentials = value.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        raise unauthorized()
    return credentials.strip()


async def _validated(request: Request, token: str, audience: Audience) -> Principal:
    lookup = database_issuers(request.app.state.db_engine)
    try:
        return await validate_token(token, audience=audience, lookup=lookup)
    except InvalidCredentials:
        raise unauthorized() from None


@dataclass(frozen=True)
class TasksCaller:
    principal: Principal
    app: api_keys.CallerApp


async def _tasks_caller(request: Request) -> TasksCaller | None:
    """Real tasks credentials (JWT + API key), or ``None`` if the request has no token."""
    token = _bearer(request)
    if token is None:
        return None
    principal = await _validated(request, token, Audience.TASKS)
    raw = request.headers.get("x-api-key")
    app = await api_keys.authenticate(request.app.state.db_engine, raw) if raw else None
    if app is None or app.workspace_id != principal.workspace_id:
        raise unauthorized()
    return TasksCaller(principal=principal, app=app)


async def _human(request: Request, audience: Audience, dev: DevPerson | None) -> Principal:
    token = _bearer(request)
    if token is not None:
        return await _validated(request, token, audience)
    if dev is not None and dev_identity.allowed(request):
        return dev_identity.person(dev, audience.value)
    raise unauthorized()


# --- tasks ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Submission:
    principal: Principal
    agent_id: str


async def submission(
    request: Request,
    dev_identity_body: Annotated[DevIdentity | None, Body(alias="dev_identity")] = None,
    agent_id: Annotated[str | None, Body(min_length=1)] = None,
) -> Submission:
    """Submit: the agent is resolved server-side from the API key's allowed set (ADR-0013)."""
    caller = await _tasks_caller(request)
    if caller is not None:
        allowed = caller.app.allowed_agents
        if agent_id is None:
            if len(allowed) != 1:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "agent_id is required: this API key may run several agents",
                )
            return Submission(principal=caller.principal, agent_id=allowed[0])
        if agent_id not in allowed:
            raise unauthorized()
        return Submission(principal=caller.principal, agent_id=agent_id)
    if dev_identity_body is not None and dev_identity.allowed(request):
        principal = dev_identity.submitter(dev_identity_body, Audience.TASKS.value)
        return Submission(principal=principal, agent_id=dev_identity_body.agent_id)
    raise unauthorized()


@dataclass(frozen=True)
class RunReader:
    scope: WorkspaceScope
    # ``None`` only for the dev shortcut, which reads any run in its workspace.
    requester: str | None


async def run_reader(
    request: Request, dev_workspace_id: Annotated[str | None, Query(min_length=1)] = None
) -> RunReader:
    """``GET /v1/tasks/runs/{id}``: with real credentials, the caller's own runs only."""
    caller = await _tasks_caller(request)
    if caller is not None:
        scope = WorkspaceScope(WorkspaceId(caller.principal.workspace_id))
        return RunReader(scope=scope, requester=caller.principal.subject)
    if dev_workspace_id is not None and dev_identity.allowed(request):
        return RunReader(scope=WorkspaceScope(WorkspaceId(dev_workspace_id)), requester=None)
    raise unauthorized()


async def tasks_principal(
    request: Request,
    dev_identity_body: Annotated[DevPerson | None, Body(alias="dev_identity", embed=True)] = None,
) -> Principal:
    """Other tasks actions (cancel): the requester, authenticated with JWT + API key."""
    caller = await _tasks_caller(request)
    if caller is not None:
        return caller.principal
    if dev_identity_body is not None and dev_identity.allowed(request):
        return dev_identity.person(dev_identity_body, Audience.TASKS.value)
    raise unauthorized()


# --- approvals and ops ----------------------------------------------------------------------


async def approvals_principal(
    request: Request,
    dev_identity_body: Annotated[DevPerson | None, Body(alias="dev_identity", embed=True)] = None,
) -> Principal:
    return await _human(request, Audience.APPROVALS, dev_identity_body)


async def ops_principal(
    request: Request,
    dev_identity_body: Annotated[DevPerson | None, Body(alias="dev_identity", embed=True)] = None,
) -> Principal:
    return await _human(request, Audience.OPS, dev_identity_body)
