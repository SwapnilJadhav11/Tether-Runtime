"""T-25 (unit half): reconciliation eligibility is policy, deny-by-default (ADR-0007, AD-03)."""

from __future__ import annotations

from tether.policy.config import WorkspaceConfig
from tether.policy.engine import evaluate_reconciliation_eligibility

ALICE = {"sub": "alice", "roles": ["operator", "reconciler"]}
DAVE = {"sub": "dave", "roles": ["reconciler"]}
BOB = {"sub": "bob", "roles": ["oncall"]}


def _workspace(policy: dict[str, object]) -> WorkspaceConfig:
    return WorkspaceConfig.model_validate(
        {"id": "ws", "agents": [{"id": "a", "workload": "w"}], "policy": policy}
    )


WITH_RECONCILERS = _workspace({"reconciliation": {"reconciler_roles": ["reconciler"]}})


def test_t25_a_reconciler_role_from_policy_is_eligible() -> None:
    assert evaluate_reconciliation_eligibility(WITH_RECONCILERS, reconciler=DAVE, requester=ALICE)


def test_t25_a_caller_without_a_reconciler_role_is_not_eligible() -> None:
    assert not evaluate_reconciliation_eligibility(
        WITH_RECONCILERS, reconciler=BOB, requester=ALICE
    )


def test_t25_the_requester_can_never_reconcile_their_own_run() -> None:
    assert not evaluate_reconciliation_eligibility(
        WITH_RECONCILERS, reconciler=ALICE, requester=ALICE
    )


def test_t25_reconciliation_is_denied_by_default() -> None:
    no_section = _workspace({"rules": []})
    assert not evaluate_reconciliation_eligibility(no_section, reconciler=DAVE, requester=ALICE)


def test_reconciliation_settings_are_part_of_the_policy_version() -> None:
    without = _workspace({"rules": []})
    assert WITH_RECONCILERS.policy_version != without.policy_version
