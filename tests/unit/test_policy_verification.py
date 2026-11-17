"""W5.1, T-27 (unit half): ``PolicyEngine.evaluate_verification`` (ADR-0022).

The verify operation is its own read-purpose action. It is allowed only when all seven of the
owner's conditions hold; each test below breaks exactly one of them and expects a ``deny`` with
the named reason. The write's ``require_approval`` rule is never authority and never a refusal.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tether.policy.config import WorkspaceConfig
from tether.policy.engine import (
    ActionContext,
    BuiltinPolicyEngine,
    PolicyEngine,
    ToolPolicyMetadata,
    TrustedAgent,
    TrustedRequester,
    VerificationRequest,
)

WS = "ws-a"
POLICY: dict[str, Any] = {
    "role_permissions": {
        "operator": ["thing:write", "thing:read"],
        "writer_only": ["thing:write"],
    },
    "agent_permissions": {
        "agent-1": ["thing:write", "thing:read"],
        "agent-writes": ["thing:write"],
    },
    "rules": [
        {"tool": "gated_write", "decision": "require_approval", "approver_roles": ["approver"]},
        {"tool": "plain_write", "decision": "allow"},
        {"tool": "blocked_write", "decision": "deny"},
    ],
    "reconciliation": {"reconciler_roles": ["reconciler"]},
}
WORKSPACE = WorkspaceConfig.model_validate(
    {
        "id": WS,
        "agents": [{"id": "agent-1", "workload": "w"}, {"id": "agent-writes", "workload": "w"}],
        "policy": POLICY,
    }
)
ENGINE = BuiltinPolicyEngine(WORKSPACE)
CALL_HASH = "a" * 64
ALICE = TrustedRequester("alice", frozenset({"operator"}))


def _tool(
    name: str = "gated_write",
    *,
    effect: str = "write",
    risk: str = "high",
    version: str = "1.0.0",
    verify: tuple[str, ...] | None = ("thing:read",),
) -> ToolPolicyMetadata:
    return ToolPolicyMetadata(
        name,
        version,
        effect,  # type: ignore[arg-type]
        risk,  # type: ignore[arg-type]
        frozenset({"thing:write"}),
        verify_permissions=None if verify is None else frozenset(verify),
    )


def request(**overrides: Any) -> VerificationRequest:
    values: dict[str, Any] = {
        "workspace_id": WS,
        "requester": ALICE,
        "agent": TrustedAgent("agent-1"),
        "tool": _tool(),
        "pinned_version": "1.0.0",
        "context": ActionContext(run_id="r-1", step_ordinal=0, call_hash=CALL_HASH),
        "probe_call_hash": CALL_HASH,
        "authorized_policy_version": ENGINE.policy_version,
    }
    return VerificationRequest(**{**values, **overrides})


def test_t27_the_builtin_engine_implements_the_four_decision_points() -> None:
    assert isinstance(ENGINE, PolicyEngine)
    assert callable(ENGINE.evaluate_verification)


def test_t27_an_approval_gated_write_is_verified_without_a_second_approval() -> None:
    decision = ENGINE.evaluate_verification(request())
    assert decision.outcome == "allow"
    assert decision.reasons == ("verification_allow",)
    assert decision.policy_version == ENGINE.policy_version
    assert decision.approver_roles == ()


def test_t27_an_allow_rule_write_is_verified_too() -> None:
    assert ENGINE.evaluate_verification(request(tool=_tool("plain_write"))).outcome == "allow"


@pytest.mark.parametrize(
    ("condition", "overrides", "reason"),
    [
        # 1, 2, 7: only the declared read-back of the exact approved version of a write.
        ("1/7 no declaration", {"tool": _tool(verify=None)}, "verify_not_declared"),
        ("1/7 not a write", {"tool": _tool(effect="read")}, "verify_not_declared"),
        ("2 another version", {"pinned_version": "0.9.0"}, "version_mismatch"),
        # 3: only the exact original action's arguments.
        ("3 other arguments", {"probe_call_hash": "b" * 64}, "target_mismatch"),
        # 4: the same workspace, requester and agent.
        ("4 other workspace", {"workspace_id": "ws-b"}, "workspace_mismatch"),
        (
            "4 no requester",
            {"requester": TrustedRequester("", frozenset({"operator"}))},
            "context_missing:requester",
        ),
        ("4 unknown agent", {"agent": TrustedAgent("agent-x")}, "context_missing:agent"),
        # 5: independently authorized for a read purpose, under current policy.
        ("5 explicit deny", {"tool": _tool("blocked_write")}, "rule_deny"),
        ("5 no rule", {"tool": _tool("unruled_write")}, "no_matching_rule"),
        (
            "5 requester lacks the read",
            {"requester": TrustedRequester("wes", frozenset({"writer_only"}))},
            "requester_lacks:thing:read",
        ),
        (
            "5 agent lacks the read",
            {"agent": TrustedAgent("agent-writes")},
            "agent_lacks:thing:read",
        ),
        # 6: no permission the original effective context did not have.
        ("6 policy changed", {"authorized_policy_version": "other"}, "policy_changed"),
        ("6 authorization unknown", {"authorized_policy_version": None}, "authorization_unknown"),
    ],
)
def test_t27_each_condition_failing_on_its_own_denies_the_probe(
    condition: str, overrides: dict[str, Any], reason: str
) -> None:
    decision = ENGINE.evaluate_verification(request(**overrides))
    assert decision.outcome == "deny", condition
    assert reason in decision.reasons, (condition, decision.reasons)


def test_t27_the_decision_set_is_allow_or_deny_only() -> None:
    """``require_approval`` is never a verify decision, whatever the write's rule says."""
    outcomes = {
        ENGINE.evaluate_verification(request(tool=_tool(name))).outcome
        for name in ("gated_write", "plain_write", "blocked_write", "unruled_write")
    }
    assert outcomes <= {"allow", "deny"}


def test_t27_risk_does_not_refuse_a_read_back() -> None:
    low = ENGINE.evaluate_verification(request(tool=_tool("plain_write", risk="low")))
    high = ENGINE.evaluate_verification(request(tool=_tool("plain_write", risk="high")))
    assert low.outcome == high.outcome == "allow"


def test_t27_missing_probe_permissions_are_not_a_declaration() -> None:
    assert ENGINE.evaluate_verification(request(tool=_tool(verify=()))).reasons == (
        "verify_not_declared",
    )


def test_t27_tool_metadata_carries_the_declared_verify_permissions() -> None:
    from pydantic import BaseModel

    from tether.sdk import ToolDescriptor, VerificationResult, VerifyDeclaration

    class In(BaseModel):
        key: str

    class Out(BaseModel):
        ok: bool

    async def handler(args: Any, ctx: Any) -> Out:
        return Out(ok=True)

    async def probe(args: Any, ctx: Any) -> VerificationResult:
        return VerificationResult("inconclusive", Out(ok=False))

    descriptor = ToolDescriptor(
        name="w",
        version="2",
        description="d",
        input_model=In,
        output_model=Out,
        effect="write",
        handler=handler,
        risk="high",
        required_permissions=("thing:write",),
        idempotency="supported",
        verify=VerifyDeclaration(probe=probe, evidence_model=Out, required_permissions=("x:y",)),
    )
    assert ToolPolicyMetadata.from_descriptor(descriptor).verify_permissions == frozenset({"x:y"})
    plain = replace(descriptor, verify=None)
    assert ToolPolicyMetadata.from_descriptor(plain).verify_permissions is None
