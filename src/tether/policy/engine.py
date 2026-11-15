"""The V1 policy engine contract and its built-in evaluator (AD-05, ADR-0009, ADR-0020).

``PolicyEngine`` has three pure decision points bound to one workspace's loaded policy: an action
decision, approval eligibility and reconciliation eligibility. Reading and comparing the active
``policy_version`` stays in the caller's decision transaction (ADR-0009 rules 5 and 6).

Inputs are trusted runtime values only (invariant 11): the run's requester snapshot (subject and
roles from the validated token), the run's agent, the run's workspace, the registry descriptor's
policy metadata and runtime metadata. Model text, rationale, raw arguments and tool output are
never inputs. ``ActionContext.trusted_attributes`` exists for future runtime-attested context; it
is empty in V1 and the built-in evaluator does not read it.

The built-in evaluator is deny by default. Requester permissions are the union of
``role_permissions`` over the requester's roles; the effective set is that union intersected with
the agent's permissions; every required permission of the tool must be in it. Risk is ``low`` or
``high``; a high-risk tool is never allowed. Separation of duties is built in: an approver or a
reconciler is never the run's requester.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from tether.policy.config import BootstrapConfigError, WorkspaceConfig
from tether.sdk import Effect, Risk, ToolDescriptor

Outcome = Literal["allow", "deny", "require_approval"]
JSONScalar = str | int | bool | None


class PolicyStaleError(Exception):
    """This process's loaded policy is not the workspace's active version: decide nothing."""


# --- trusted inputs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedRequester:
    """A person: the subject and roles of a validated token (for a run, its stored snapshot)."""

    subject: str
    roles: frozenset[str]

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> TrustedRequester:
        """From ``runs.requester_snapshot`` (or a principal's ``requester``). A malformed subject
        becomes empty, which the evaluator treats as missing context; non-string roles are
        ignored."""
        subject = snapshot.get("sub")
        roles = snapshot.get("roles")
        return cls(
            subject=subject if isinstance(subject, str) else "",
            roles=frozenset(r for r in roles if isinstance(r, str))
            if isinstance(roles, list | tuple)
            else frozenset(),
        )


@dataclass(frozen=True)
class TrustedAgent:
    """The run's agent, bound by the API key at submission (TX1)."""

    agent_id: str


@dataclass(frozen=True)
class ToolPolicyMetadata:
    """The policy-relevant part of a registry descriptor; never derived from a call."""

    name: str
    version: str
    effect: Effect
    risk: Risk
    required_permissions: frozenset[str]

    @classmethod
    def from_descriptor(cls, descriptor: ToolDescriptor) -> ToolPolicyMetadata:
        return cls(
            name=descriptor.name,
            version=descriptor.version,
            effect=descriptor.effect,
            risk=descriptor.risk,
            required_permissions=frozenset(descriptor.required_permissions),
        )


def _attributes(values: Mapping[str, Any]) -> Mapping[str, JSONScalar]:
    checked: dict[str, JSONScalar] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key:
            raise ValueError("a trusted attribute key must be a non-empty string")
        if value is not None and type(value) not in (str, int, bool):
            raise ValueError(f"trusted attribute {key!r} must be a JSON scalar (no floats)")
        checked[key] = value
    return MappingProxyType(checked)


@dataclass(frozen=True)
class ActionContext:
    """Runtime metadata of the call being decided.

    ``trusted_attributes`` (ADR-0020 rule 7) is for runtime-attested context only: Tether's
    runtime may populate it from authoritative or explicitly verified sources, never by copying
    raw model arguments. It is empty in V1 and unused by the built-in evaluator.
    """

    run_id: str
    step_ordinal: int
    call_hash: str
    trusted_attributes: Mapping[str, JSONScalar] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "trusted_attributes", _attributes(self.trusted_attributes))


@dataclass(frozen=True)
class ActionRequest:
    workspace_id: str
    requester: TrustedRequester
    agent: TrustedAgent
    tool: ToolPolicyMetadata
    context: ActionContext


@dataclass(frozen=True)
class ApprovalEligibilityRequest:
    workspace_id: str
    approver: TrustedRequester
    requester: TrustedRequester
    tool_name: str


@dataclass(frozen=True)
class ReconciliationEligibilityRequest:
    workspace_id: str
    reconciler: TrustedRequester
    requester: TrustedRequester
    tool_name: str


# --- answers ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Outcome
    reasons: tuple[str, ...]
    policy_version: str
    # Non-empty if and only if the outcome is require_approval.
    approver_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]
    policy_version: str


# --- the contract -----------------------------------------------------------------------------


@runtime_checkable
class PolicyEngine(Protocol):
    @property
    def workspace_id(self) -> str: ...

    @property
    def policy_version(self) -> str: ...

    def evaluate_action(self, request: ActionRequest) -> PolicyDecision: ...

    def evaluate_approval_eligibility(self, request: ApprovalEligibilityRequest) -> Eligibility: ...

    def evaluate_reconciliation_eligibility(
        self, request: ReconciliationEligibilityRequest
    ) -> Eligibility: ...


# --- the built-in V1 evaluator ----------------------------------------------------------------


class BuiltinPolicyEngine:
    """Declarative, deny-by-default evaluator over one workspace's bootstrap policy (ADR-0020)."""

    def __init__(self, workspace: WorkspaceConfig) -> None:
        self._workspace = workspace
        self._policy = workspace.policy

    @property
    def workspace_id(self) -> str:
        return self._workspace.id

    @property
    def policy_version(self) -> str:
        return self._workspace.policy_version

    def _deny(self, *reasons: str) -> PolicyDecision:
        return PolicyDecision("deny", tuple(reasons), self.policy_version)

    def _answer(self, eligible: bool, *reasons: str) -> Eligibility:
        return Eligibility(eligible, tuple(reasons), self.policy_version)

    def _role_permissions(self, roles: frozenset[str]) -> frozenset[str]:
        granted: set[str] = set()
        for role in roles:
            granted.update(self._policy.role_permissions.get(role, ()))
        return frozenset(granted)

    def evaluate_action(self, request: ActionRequest) -> PolicyDecision:
        if request.workspace_id != self.workspace_id:
            return self._deny("workspace_mismatch")
        if not request.requester.subject:
            return self._deny("context_missing:requester")
        agent_permissions = self._policy.agent_permissions.get(request.agent.agent_id)
        if agent_permissions is None:
            return self._deny("context_missing:agent")
        required = request.tool.required_permissions
        if not required:
            return self._deny("context_missing:tool_permissions")
        requester_permissions = self._role_permissions(request.requester.roles)
        lacking = [f"agent_lacks:{p}" for p in sorted(required - set(agent_permissions))]
        lacking += [f"requester_lacks:{p}" for p in sorted(required - requester_permissions)]
        if lacking:
            return self._deny(*lacking)
        rule = self._policy.rule_for(request.tool.name)
        if rule is None:
            return self._deny("no_matching_rule")
        if rule.decision == "deny":
            return self._deny("rule_deny")
        if rule.decision == "allow":
            if request.tool.risk == "high":
                return self._deny("risk_requires_approval")
            return PolicyDecision("allow", ("rule_allow",), self.policy_version)
        return PolicyDecision(
            "require_approval",
            ("rule_require_approval",),
            self.policy_version,
            approver_roles=rule.approver_roles,
        )

    def evaluate_approval_eligibility(self, request: ApprovalEligibilityRequest) -> Eligibility:
        if request.workspace_id != self.workspace_id:
            return self._answer(False, "workspace_mismatch")
        if not request.approver.subject or not request.requester.subject:
            return self._answer(False, "context_missing:approver")
        if request.approver.subject == request.requester.subject:
            return self._answer(False, "self_approval")
        rule = self._policy.rule_for(request.tool_name)
        if rule is None or rule.decision != "require_approval":
            return self._answer(False, "not_approval_required")
        if not request.approver.roles & set(rule.approver_roles):
            return self._answer(False, "role_not_eligible")
        return self._answer(True, "eligible")

    def evaluate_reconciliation_eligibility(
        self, request: ReconciliationEligibilityRequest
    ) -> Eligibility:
        if request.workspace_id != self.workspace_id:
            return self._answer(False, "workspace_mismatch")
        if not request.reconciler.subject or not request.requester.subject:
            return self._answer(False, "context_missing:reconciler")
        if request.reconciler.subject == request.requester.subject:
            return self._answer(False, "self_reconciliation")
        if not request.reconciler.roles & set(self._policy.reconciliation.reconciler_roles):
            return self._answer(False, "role_not_eligible")
        return self._answer(True, "eligible")


def check_tools_against_policy(
    workspace: WorkspaceConfig, tools: Mapping[str, ToolPolicyMetadata]
) -> None:
    """Startup check (ADR-0020 rule 4): no rule may allow a high-risk tool. Fails closed."""
    for rule in workspace.policy.rules:
        tool = tools.get(rule.tool)
        if rule.decision == "allow" and tool is not None and tool.risk == "high":
            raise BootstrapConfigError(
                f"workspace {workspace.id}: policy allows high-risk tool {rule.tool};"
                " a high-risk tool needs require_approval or deny"
            )
