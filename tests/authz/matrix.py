"""T-24: the expected authorization table as data (PRD "Authorization for reads and human actions").

One ``RouteSpec`` per route. ``allow`` states, for credentials that are valid for the route's own
audience in the target's own workspace, the status each identity gets; it must name every identity
in ``IDENTITIES`` so that no combination is left to a default. Everything else follows the order
of ADR-0013 and is expanded by ``cases``:

* a credential that is not valid (none, bad signature, expired, malformed) → ``401``;
* a valid token for another audience, however privileged → ``401`` (cross-audience reuse);
* tasks routes also need the workspace API key: absent, or the other workspace's → ``401``;
* valid credentials of the *other* workspace → ``404`` (existence is never disclosed);
* valid credentials, right audience and workspace: the ``allow`` entry (role, ownership, policy).

``alice`` submitted every target run; ``carol`` is another user of the same workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from tests.authz.fixtures import (
    APP_KEYS,
    APPROVALS,
    AUDIENCES,
    FIXTURE_B,
    OPS,
    OPS_DEMO,
    STRANGER,
    TASKS,
    WORKSPACES,
    headers,
    token,
)

# identity name -> (subject, roles). ``alice`` is the requester of every target run.
IDENTITIES: dict[str, tuple[str, list[str]]] = {
    "requester": ("alice", ["operator"]),
    "other user": ("carol", ["operator"]),
    "other user holding every role": (
        "carol",
        ["operator", "approver", "oncall", "reconciler", "sre", "auditor", "workspace_admin"],
    ),
    "no roles": ("nobody", []),
    "approver (policy role missing)": ("ann", ["approver"]),
    "approver + oncall": ("bob", ["approver", "oncall"]),
    "oncall without the approver role": ("olga", ["oncall"]),
    "reconciler (policy role missing)": ("dave", ["reconciler"]),
    "reconciler + sre": ("rita", ["reconciler", "sre"]),
    "sre without the reconciler role": ("sam", ["sre"]),
    "auditor": ("audrey", ["auditor"]),
    "workspace_admin": ("adam", ["workspace_admin"]),
    "requester holding approver + oncall": ("alice", ["approver", "oncall"]),
    "requester holding reconciler + sre": ("alice", ["reconciler", "sre"]),
}
# A token that holds every role, used to show no role rescues the wrong audience.
EVERY_ROLE = (
    "root",
    ["operator", "approver", "oncall", "reconciler", "sre", "auditor", "workspace_admin"],
)

Target = Literal["run", "approval", "ledger"]
Credential = Literal["valid", "none", "bad signature", "expired", "malformed"]
INVALID_CREDENTIALS: tuple[Credential, ...] = ("none", "bad signature", "expired", "malformed")


@dataclass(frozen=True)
class RouteSpec:
    method: str
    path: str
    audience: str
    allow: dict[str, int]
    target: Target | None = None
    fixed: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    # Status of the identities that pass authorization and act on their own workspace.
    note: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)

    @property
    def label(self) -> str:
        suffix = "".join(f" [{v}]" for v in self.fixed.values())
        return f"{self.method} {self.path}{suffix}"

    @property
    def needs_api_key(self) -> bool:
        return self.audience == TASKS


def _only(identities: list[str], status: int, otherwise: int = 404) -> dict[str, int]:
    assert set(identities) <= set(IDENTITIES)
    return {name: (status if name in identities else otherwise) for name in IDENTITIES}


def _allow(grants: dict[str, int], otherwise: int = 404) -> dict[str, int]:
    return {name: grants.get(name, otherwise) for name in IDENTITIES}


# On the tasks routes ownership is the token's subject, whatever roles it carries: every identity
# whose subject is ``alice`` is the requester.
REQUESTERS = [
    "requester",
    "requester holding approver + oncall",
    "requester holding reconciler + sre",
]
READ_RUN = dict.fromkeys(REQUESTERS, 200)
EVERY = "other user holding every role"
# Eligible to decide an approval: the ``approver`` role, the policy role ``oncall`` (bootstrap
# ``approver_roles``) and not the run's requester (separation of duties).
APPROVER = ["approver + oncall", EVERY]
# May open the approvals list: the ``approver`` role (the list then holds only what they could
# decide, so an approver without the policy role gets an empty page).
LISTERS = [
    *APPROVER,
    "approver (policy role missing)",
    "requester holding approver + oncall",
]

ROUTES: tuple[RouteSpec, ...] = (
    # --- tether-tasks + API key: the requester -----------------------------------------------
    RouteSpec(
        "POST",
        "/v1/tasks/runs",
        TASKS,
        # Any validated end user may submit: nothing about a run exists to be out of scope.
        allow=dict.fromkeys(IDENTITIES, 201),
        body={"task_input": {"message": "go #scenario=read"}},
        note="submit: any validated user; the agent must be in the API key's allowed set",
    ),
    RouteSpec("GET", "/v1/tasks/runs/{run_id}", TASKS, _allow(READ_RUN), target="run"),
    RouteSpec(
        "POST",
        "/v1/tasks/runs/{run_id}/cancel",
        TASKS,
        _allow(dict.fromkeys(REQUESTERS, 202)),
        target="run",
    ),
    # --- tether-approvals: the approver ------------------------------------------------------
    RouteSpec(
        "POST",
        "/v1/approvals/{approval_id}/{action}",
        APPROVALS,
        _only(APPROVER, 200),
        target="approval",
        fixed={"action": "approve"},
        body={"reason": "ok"},
    ),
    RouteSpec(
        "POST",
        "/v1/approvals/{approval_id}/{action}",
        APPROVALS,
        _only(APPROVER, 200),
        target="approval",
        fixed={"action": "reject"},
        body={"reason": "no"},
    ),
    RouteSpec("GET", "/v1/approvals/approvals", APPROVALS, _only(LISTERS, 200)),
    RouteSpec(
        "GET",
        "/v1/approvals/approvals/{approval_id}",
        APPROVALS,
        _only(APPROVER, 200),
        target="approval",
    ),
    RouteSpec("GET", "/v1/approvals/runs/{run_id}", APPROVALS, _only(APPROVER, 200), target="run"),
    # --- tether-ops: reconciler, auditor, workspace_admin ------------------------------------
    RouteSpec(
        "POST",
        "/v1/ops/ledger/{ledger_id}/reconcile",
        OPS,
        # The ``reconciler`` role, the policy role ``sre`` (``reconciler_roles``), never the
        # run's requester.
        _only(["reconciler + sre", EVERY], 200),
        target="ledger",
        body={"outcome": "not_applied", "justification": "checked the target"},
    ),
    RouteSpec(
        "GET",
        "/v1/ops/runs/{run_id}",
        OPS,
        # The PRD table gives run inspection to auditors and workspace admins only; the
        # reconciler and approver roles on this audience are not listed, so they fail closed.
        _only(["auditor", "workspace_admin", EVERY], 200),
        target="run",
    ),
    RouteSpec(
        "POST",
        "/v1/ops/runs/{run_id}/cancel",
        OPS,
        # Approvers cannot cancel (they reject); only ``workspace_admin`` may, for any run.
        _only(["workspace_admin", EVERY], 202),
        target="run",
    ),
)

# Routes that exist without any credential. Each carries no tenant data.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/openapi.json")}
)
MATRIX_ROUTES: frozenset[tuple[str, str]] = frozenset(spec.key for spec in ROUTES)


@dataclass(frozen=True)
class Case:
    """One request: whose credentials, for whose data, and the status the table demands."""

    what: str
    credential: Credential
    audience: str
    cred_ws: str
    target_ws: str
    sub: str
    roles: tuple[str, ...]
    api_key: Literal["own", "foreign", "none"] | None
    expected: int
    unknown_target: bool = False

    @property
    def permitted(self) -> bool:
        return self.expected < 300

    def headers(self) -> dict[str, str]:
        other = FIXTURE_B if self.cred_ws == OPS_DEMO else OPS_DEMO
        key = {
            "own": APP_KEYS[self.cred_ws].raw,
            "foreign": APP_KEYS[other].raw,
            "none": None,
            None: None,
        }[self.api_key]
        match self.credential:
            case "none":
                return headers(None, key)
            case "malformed":
                return {"Authorization": "Token abc", **headers(None, key)}
            case "bad signature":
                bearer = token(self.cred_ws, self.audience, self.sub, self.roles, key=STRANGER)
            case "expired":
                bearer = token(self.cred_ws, self.audience, self.sub, self.roles, exp=1)
            case _:
                bearer = token(self.cred_ws, self.audience, self.sub, self.roles)
        return headers(bearer, key)


def cases(spec: RouteSpec, target_ws: str) -> list[Case]:
    """Every credential state, identity and workspace for ``spec`` against ``target_ws`` data."""
    assert set(spec.allow) == set(IDENTITIES), f"{spec.label}: every identity needs an expectation"
    other = FIXTURE_B if target_ws == OPS_DEMO else OPS_DEMO
    key = "own" if spec.needs_api_key else None
    found: list[Case] = []

    def add(
        what: str,
        expected: int,
        *,
        audience: str | None = None,
        cred_ws: str | None = None,
        identity: tuple[str, list[str]] = ("alice", ["operator"]),
        credential: Credential = "valid",
        api_key: Literal["own", "foreign", "none"] | None = key,
        unknown_target: bool = False,
    ) -> None:
        found.append(
            Case(
                what=what,
                credential=credential,
                audience=audience or spec.audience,
                cred_ws=cred_ws or target_ws,
                target_ws=target_ws,
                sub=identity[0],
                roles=tuple(identity[1]),
                api_key=api_key,
                expected=expected,
                unknown_target=unknown_target,
            )
        )

    for name, expected in spec.allow.items():
        add(f"{name}", expected, identity=IDENTITIES[name])
        if spec.target is not None:  # another workspace's credentials: out of scope, not a 401
            add(
                f"{name}, credentials of {other}",
                404,
                identity=IDENTITIES[name],
                cred_ws=other,
                api_key="own" if spec.needs_api_key else None,
            )
    privileged = IDENTITIES[_most_privileged(spec)]
    if spec.target is not None:
        add("a permitted identity, unknown id", 404, identity=privileged, unknown_target=True)
    for audience in AUDIENCES:
        if audience != spec.audience:
            add(
                f"a {audience} token holding every role",
                401,
                audience=audience,
                identity=EVERY_ROLE,
            )
    for credential in INVALID_CREDENTIALS:
        add(f"{credential} credential", 401, identity=privileged, credential=credential)
    if spec.needs_api_key:
        add("no API key", 401, identity=privileged, api_key="none")
        add("the other workspace's API key", 401, identity=privileged, api_key="foreign")
        add(
            f"{other}'s token with {target_ws}'s API key",
            401,
            identity=privileged,
            cred_ws=other,
            api_key="foreign",
        )
    else:
        # A tasks API key sent alongside makes no difference to these audiences.
        add(
            "a permitted identity that also sends an API key",
            spec.allow[_most_privileged(spec)],
            identity=privileged,
            api_key="own",
        )
    return found


def _most_privileged(spec: RouteSpec) -> str:
    """An identity the table lets through, or the requester where nobody is refused nothing."""
    for name, status in spec.allow.items():
        if status < 300:
            return name
    raise AssertionError(f"{spec.label}: the table grants nobody anything")


__all__ = ["MATRIX_ROUTES", "PUBLIC_ROUTES", "ROUTES", "WORKSPACES", "Case", "RouteSpec", "cases"]
