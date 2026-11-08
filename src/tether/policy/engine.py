"""Built-in deny-by-default policy evaluator (AD-05, ADR-0009).

Inputs come from the registry descriptor and the run's requester snapshot, never from model
output (invariant 11). Rules are checked in order; the first matching rule decides; no match
means deny. Decisions are only made under the workspace's active policy version.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from tether.policy.config import PolicyRule, WorkspaceConfig

Outcome = Literal["allow", "deny", "require_approval"]


class PolicyStaleError(Exception):
    """This process's loaded policy is not the workspace's active version: decide nothing."""


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reasons: tuple[str, ...]
    policy_version: str
    approver_roles: tuple[str, ...] = ()


def _roles(person: Mapping[str, Any]) -> set[str]:
    roles = person.get("roles", [])
    return {role for role in roles if isinstance(role, str)}


def _matches(rule: PolicyRule, tool: str, requester: Mapping[str, Any]) -> bool:
    if rule.tool != tool:
        return False
    return rule.requester_roles is None or bool(_roles(requester) & set(rule.requester_roles))


def evaluate_action(
    workspace: WorkspaceConfig, *, tool: str, requester: Mapping[str, Any]
) -> Decision:
    for index, rule in enumerate(workspace.policy.rules):
        if _matches(rule, tool, requester):
            return Decision(
                outcome=rule.decision,
                reasons=(f"rule {index} ({rule.decision})",),
                policy_version=workspace.policy_version,
                approver_roles=rule.approver_roles,
            )
    return Decision("deny", ("no matching rule",), workspace.policy_version)


def evaluate_reconciliation_eligibility(
    workspace: WorkspaceConfig, *, reconciler: Mapping[str, Any], requester: Mapping[str, Any]
) -> bool:
    """A reconciler needs a configured reconciler role and must not be the requester (ADR-0007).

    Deny-by-default: a policy without reconciler roles lets nobody reconcile.
    """
    if reconciler.get("sub") == requester.get("sub"):
        return False
    return bool(_roles(reconciler) & set(workspace.policy.reconciliation.reconciler_roles))


def evaluate_approval_eligibility(
    decision: Decision, *, approver: Mapping[str, Any], requester: Mapping[str, Any]
) -> bool:
    """An approver needs a listed role and must not be the requester (separation of duties)."""
    if decision.outcome != "require_approval":
        return False
    if approver.get("sub") == requester.get("sub"):
        return False
    return bool(_roles(approver) & set(decision.approver_roles))
