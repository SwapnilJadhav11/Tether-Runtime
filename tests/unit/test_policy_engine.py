"""W3.1, T-06 (unit half): the V1 ``PolicyEngine`` contract and its built-in evaluator.

ADR-0009 and ADR-0020: deny by default; requester permissions (from roles) ∩ agent permissions
must contain every required permission of the tool; risk is ``low``/``high`` and a high-risk tool
is never allowed; one rule per tool; approver and reconciler are never the requester. Inputs are
trusted runtime values only; ``trusted_attributes`` is empty in V1 and ignored by the built-in
evaluator.
"""

from __future__ import annotations

from typing import Any

import pytest

from tether.policy.config import BootstrapConfigError, WorkspaceConfig
from tether.policy.engine import (
    ActionContext,
    ActionRequest,
    ApprovalEligibilityRequest,
    BuiltinPolicyEngine,
    PolicyEngine,
    ReconciliationEligibilityRequest,
    ToolPolicyMetadata,
    TrustedAgent,
    TrustedRequester,
    check_tools_against_policy,
)

WS = "ws-a"
POLICY: dict[str, Any] = {
    "role_permissions": {
        "operator": ["thing:read", "thing:write", "log:read"],
        "viewer": ["thing:read"],
    },
    "agent_permissions": {
        "agent-1": ["thing:read", "thing:write", "log:read"],
        "agent-2": ["thing:read"],
    },
    "rules": [
        {"tool": "read_thing", "decision": "allow"},
        {"tool": "write_thing", "decision": "require_approval", "approver_roles": ["approver"]},
        {"tool": "two_perm_thing", "decision": "allow"},
        {"tool": "blocked_thing", "decision": "deny"},
        {"tool": "risky_allowed", "decision": "allow"},
    ],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
}
WORKSPACE = WorkspaceConfig.model_validate(
    {
        "id": WS,
        "agents": [{"id": "agent-1", "workload": "w"}, {"id": "agent-2", "workload": "w"}],
        "policy": POLICY,
    }
)
ENGINE = BuiltinPolicyEngine(WORKSPACE)


def _tool(name: str, effect: str, risk: str, *permissions: str) -> ToolPolicyMetadata:
    return ToolPolicyMetadata(name, "1", effect, risk, frozenset(permissions))  # type: ignore[arg-type]


TOOLS = {
    "read_thing": _tool("read_thing", "read", "low", "thing:read"),
    "write_thing": _tool("write_thing", "write", "high", "thing:write"),
    "two_perm_thing": _tool("two_perm_thing", "read", "low", "thing:read", "log:read"),
    "blocked_thing": _tool("blocked_thing", "read", "low", "thing:read"),
    "risky_allowed": _tool("risky_allowed", "write", "high", "thing:read"),
    "unruled_thing": _tool("unruled_thing", "read", "low", "thing:read"),
}
ALICE = TrustedRequester("alice", frozenset({"operator"}))
VIEWER = TrustedRequester("vera", frozenset({"viewer"}))
BOB = TrustedRequester("bob", frozenset({"approver"}))
CONTEXT = ActionContext(run_id="r-1", step_ordinal=0, call_hash="a" * 64)


def action(
    tool: str,
    *,
    requester: TrustedRequester = ALICE,
    agent: str = "agent-1",
    workspace: str = WS,
    context: ActionContext = CONTEXT,
) -> ActionRequest:
    return ActionRequest(
        workspace_id=workspace,
        requester=requester,
        agent=TrustedAgent(agent),
        tool=TOOLS[tool],
        context=context,
    )


# --- T-06 decision table: evaluate_action ---------------------------------------------------

T06: list[tuple[str, ActionRequest, str, tuple[str, ...]]] = [
    ("both permitted, low risk", action("read_thing"), "allow", ("rule_allow",)),
    (
        "requester lacks the permission",
        action("write_thing", requester=VIEWER),
        "deny",
        ("requester_lacks:thing:write",),
    ),
    (
        "agent lacks the permission",
        action("write_thing", agent="agent-2"),
        "deny",
        ("agent_lacks:thing:write",),
    ),
    (
        "requester and agent both lack it",
        action("write_thing", requester=VIEWER, agent="agent-2"),
        "deny",
        ("agent_lacks:thing:write", "requester_lacks:thing:write"),
    ),
    (
        "one of several required permissions missing",
        action("two_perm_thing", requester=VIEWER),
        "deny",
        ("requester_lacks:log:read",),
    ),
    (
        "an unknown role grants nothing",
        action("read_thing", requester=TrustedRequester("gus", frozenset({"ghost"}))),
        "deny",
        ("requester_lacks:thing:read",),
    ),
    (
        "high risk, require_approval",
        action("write_thing"),
        "require_approval",
        ("rule_require_approval",),
    ),
    (
        "high risk with an allow rule (runtime defence)",
        action("risky_allowed"),
        "deny",
        ("risk_requires_approval",),
    ),
    ("explicit deny rule", action("blocked_thing"), "deny", ("rule_deny",)),
    ("no matching rule", action("unruled_thing"), "deny", ("no_matching_rule",)),
    (
        "workspace mismatch",
        action("read_thing", workspace="ws-b"),
        "deny",
        ("workspace_mismatch",),
    ),
    (
        "missing requester subject",
        action("read_thing", requester=TrustedRequester("", frozenset({"operator"}))),
        "deny",
        ("context_missing:requester",),
    ),
    (
        "agent without a permissions entry",
        action("read_thing", agent="agent-9"),
        "deny",
        ("context_missing:agent",),
    ),
]


@pytest.mark.parametrize(("case", "request_", "outcome", "reasons"), T06, ids=[c[0] for c in T06])
def test_t06_action_decisions(
    case: str, request_: ActionRequest, outcome: str, reasons: tuple[str, ...]
) -> None:
    decision = ENGINE.evaluate_action(request_)
    assert (decision.outcome, decision.reasons) == (outcome, reasons), case
    assert decision.policy_version == WORKSPACE.policy_version
    expected_roles = ("approver",) if outcome == "require_approval" else ()
    assert decision.approver_roles == expected_roles


def test_t06_a_tool_without_required_permissions_is_denied() -> None:
    bare = _tool("read_thing", "read", "low")
    decision = ENGINE.evaluate_action(
        ActionRequest(WS, ALICE, TrustedAgent("agent-1"), bare, CONTEXT)
    )
    assert (decision.outcome, decision.reasons) == ("deny", ("context_missing:tool_permissions",))


def test_the_engine_satisfies_the_protocol() -> None:
    assert isinstance(ENGINE, PolicyEngine)
    assert ENGINE.workspace_id == WS
    assert ENGINE.policy_version == WORKSPACE.policy_version


# --- trusted inputs ---------------------------------------------------------------------------


def test_trusted_attributes_are_empty_immutable_scalars_and_ignored_in_v1() -> None:
    assert dict(CONTEXT.trusted_attributes) == {}
    attested = ActionContext(
        run_id="r-1",
        step_ordinal=0,
        call_hash="a" * 64,
        trusted_attributes={"resource": "x", "count": 2, "flag": True, "none": None},
    )
    with pytest.raises(TypeError):
        attested.trusted_attributes["resource"] = "y"  # type: ignore[index]
    # The built-in V1 evaluator does not consume them: the same decision either way.
    plain = ENGINE.evaluate_action(action("write_thing"))
    with_attributes = ENGINE.evaluate_action(action("write_thing", context=attested))
    assert plain == with_attributes


@pytest.mark.parametrize("value", [1.5, [1], {"a": 1}, b"x"])
def test_trusted_attributes_accept_only_json_scalars(value: Any) -> None:
    with pytest.raises(ValueError, match="scalar"):
        ActionContext(
            run_id="r", step_ordinal=0, call_hash="a" * 64, trusted_attributes={"k": value}
        )


def test_trusted_attribute_keys_must_be_non_empty_strings() -> None:
    with pytest.raises(ValueError, match="key"):
        ActionContext(run_id="r", step_ordinal=0, call_hash="a" * 64, trusted_attributes={"": 1})


def test_a_requester_comes_from_the_run_snapshot_and_ignores_non_string_roles() -> None:
    assert TrustedRequester.from_snapshot({"sub": "alice", "roles": ["a", 3, "b"]}) == (
        TrustedRequester("alice", frozenset({"a", "b"}))
    )
    assert TrustedRequester.from_snapshot({"roles": "approver"}) == TrustedRequester(
        "", frozenset()
    )


# --- approval eligibility -------------------------------------------------------------------

APPROVAL_CASES: list[tuple[str, TrustedRequester, str, str, bool, tuple[str, ...]]] = [
    ("eligible approver", BOB, "write_thing", WS, True, ("eligible",)),
    (
        "requester == approver",
        TrustedRequester("alice", frozenset({"approver"})),
        "write_thing",
        WS,
        False,
        ("self_approval",),
    ),
    (
        "approver lacks a listed role",
        TrustedRequester("bob", frozenset({"operator"})),
        "write_thing",
        WS,
        False,
        ("role_not_eligible",),
    ),
    ("tool does not require approval", BOB, "read_thing", WS, False, ("not_approval_required",)),
    ("tool without a rule", BOB, "unruled_thing", WS, False, ("not_approval_required",)),
    ("workspace mismatch", BOB, "write_thing", "ws-b", False, ("workspace_mismatch",)),
    (
        "missing approver subject",
        TrustedRequester("", frozenset({"approver"})),
        "write_thing",
        WS,
        False,
        ("context_missing:approver",),
    ),
]


@pytest.mark.parametrize(
    ("case", "approver", "tool", "workspace", "eligible", "reasons"),
    APPROVAL_CASES,
    ids=[c[0] for c in APPROVAL_CASES],
)
def test_t06_approval_eligibility(
    case: str,
    approver: TrustedRequester,
    tool: str,
    workspace: str,
    eligible: bool,
    reasons: tuple[str, ...],
) -> None:
    answer = ENGINE.evaluate_approval_eligibility(
        ApprovalEligibilityRequest(
            workspace_id=workspace, approver=approver, requester=ALICE, tool_name=tool
        )
    )
    assert (answer.eligible, answer.reasons) == (eligible, reasons), case
    assert answer.policy_version == WORKSPACE.policy_version


# --- reconciliation eligibility -------------------------------------------------------------

RECONCILIATION_CASES: list[tuple[str, TrustedRequester, bool, tuple[str, ...]]] = [
    (
        "eligible reconciler",
        TrustedRequester("dave", frozenset({"reconciler"})),
        True,
        ("eligible",),
    ),
    (
        "requester == reconciler",
        TrustedRequester("alice", frozenset({"reconciler"})),
        False,
        ("self_reconciliation",),
    ),
    (
        "reconciler lacks the role",
        TrustedRequester("dave", frozenset({"operator"})),
        False,
        ("role_not_eligible",),
    ),
    (
        "missing reconciler subject",
        TrustedRequester("", frozenset({"reconciler"})),
        False,
        ("context_missing:reconciler",),
    ),
]


@pytest.mark.parametrize(
    ("case", "reconciler", "eligible", "reasons"),
    RECONCILIATION_CASES,
    ids=[c[0] for c in RECONCILIATION_CASES],
)
def test_t06_reconciliation_eligibility(
    case: str, reconciler: TrustedRequester, eligible: bool, reasons: tuple[str, ...]
) -> None:
    answer = ENGINE.evaluate_reconciliation_eligibility(
        ReconciliationEligibilityRequest(
            workspace_id=WS, reconciler=reconciler, requester=ALICE, tool_name="write_thing"
        )
    )
    assert (answer.eligible, answer.reasons) == (eligible, reasons), case


def test_reconciliation_is_denied_by_default() -> None:
    bare = WorkspaceConfig.model_validate(
        {"id": WS, "agents": [{"id": "agent-1", "workload": "w"}]}
    )
    answer = BuiltinPolicyEngine(bare).evaluate_reconciliation_eligibility(
        ReconciliationEligibilityRequest(
            workspace_id=WS,
            reconciler=TrustedRequester("dave", frozenset({"reconciler"})),
            requester=ALICE,
            tool_name="write_thing",
        )
    )
    assert (answer.eligible, answer.reasons) == (False, ("role_not_eligible",))


def test_reconciliation_in_another_workspace_is_not_eligible() -> None:
    answer = ENGINE.evaluate_reconciliation_eligibility(
        ReconciliationEligibilityRequest(
            workspace_id="ws-b",
            reconciler=TrustedRequester("dave", frozenset({"reconciler"})),
            requester=ALICE,
            tool_name="write_thing",
        )
    )
    assert (answer.eligible, answer.reasons) == (False, ("workspace_mismatch",))


# --- startup refusal of an allowed high-risk tool -------------------------------------------


def test_a_policy_that_allows_a_high_risk_tool_is_refused() -> None:
    with pytest.raises(BootstrapConfigError, match="risky_allowed"):
        check_tools_against_policy(WORKSPACE, TOOLS)


def test_a_policy_that_gates_high_risk_tools_behind_approval_is_accepted() -> None:
    safe = {name: tool for name, tool in TOOLS.items() if name != "risky_allowed"}
    check_tools_against_policy(WORKSPACE, safe)
