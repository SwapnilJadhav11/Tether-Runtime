"""T-25 (unit half): reconciliation eligibility is policy, deny-by-default (ADR-0007, ADR-0020).

Uses the ``PolicyEngine`` contract (W3.1); the full eligibility table is in
``test_policy_engine.py``.
"""

from __future__ import annotations

from tether.policy.config import WorkspaceConfig
from tether.policy.engine import (
    BuiltinPolicyEngine,
    ReconciliationEligibilityRequest,
    TrustedRequester,
)

ALICE = TrustedRequester("alice", frozenset({"operator", "reconciler"}))
DAVE = TrustedRequester("dave", frozenset({"reconciler"}))
BOB = TrustedRequester("bob", frozenset({"oncall"}))


def _workspace(policy: dict[str, object]) -> WorkspaceConfig:
    return WorkspaceConfig.model_validate(
        {"id": "ws", "agents": [{"id": "a", "workload": "w"}], "policy": policy}
    )


WITH_RECONCILERS = _workspace({"reconciliation": {"reconciler_roles": ["reconciler"]}})


def _eligible(workspace: WorkspaceConfig, reconciler: TrustedRequester) -> bool:
    return (
        BuiltinPolicyEngine(workspace)
        .evaluate_reconciliation_eligibility(
            ReconciliationEligibilityRequest(
                workspace_id="ws", reconciler=reconciler, requester=ALICE, tool_name="probe"
            )
        )
        .eligible
    )


def test_t25_a_reconciler_role_from_policy_is_eligible() -> None:
    assert _eligible(WITH_RECONCILERS, DAVE)


def test_t25_a_caller_without_a_reconciler_role_is_not_eligible() -> None:
    assert not _eligible(WITH_RECONCILERS, BOB)


def test_t25_the_requester_can_never_reconcile_their_own_run() -> None:
    assert not _eligible(WITH_RECONCILERS, ALICE)


def test_t25_reconciliation_is_denied_by_default() -> None:
    assert not _eligible(_workspace({"rules": []}), DAVE)


def test_reconciliation_settings_are_part_of_the_policy_version() -> None:
    without = _workspace({"rules": []})
    assert WITH_RECONCILERS.policy_version != without.policy_version
