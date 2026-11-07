"""Bootstrap configuration file: workspaces, agents and declarative policy (ADR-0014, ADR-0009).

Policy comes only from this file (invariant 11). Each workspace's policy has a content hash,
``policy_version`` = SHA-256 of the RFC 8785 canonical JSON of its ``policy`` section.
``tether bootstrap apply`` records and activates that version; api and worker processes load
the same file and only decide under the version that is active in the database.
"""

from __future__ import annotations

import hashlib
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tether.core.canonical import canonicalize

NonEmpty = Field(min_length=1)


class BootstrapConfigError(ValueError):
    """The bootstrap file cannot be read, parsed or validated."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicyRule(_Strict):
    """Matches one tool. Rules are checked in order; no matching rule means deny."""

    tool: str = NonEmpty
    decision: Literal["allow", "require_approval", "deny"]
    # If set, the rule matches only requesters holding one of these roles.
    requester_roles: tuple[str, ...] | None = None
    # For require_approval: an approver needs one of these roles (and must not be the requester).
    approver_roles: tuple[str, ...] = ()


class PolicyConfig(_Strict):
    rules: tuple[PolicyRule, ...] = ()


class AgentConfig(_Strict):
    id: str = NonEmpty
    workload: str = NonEmpty


class WorkspaceConfig(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, ignored_types=(cached_property,))

    id: str = NonEmpty
    agents: tuple[AgentConfig, ...] = ()
    policy: PolicyConfig = PolicyConfig()

    @cached_property
    def policy_version(self) -> str:
        canonical = canonicalize(self.policy.model_dump(mode="json"), allow_floats=False)
        return hashlib.sha256(canonical).hexdigest()

    def agent(self, agent_id: str) -> AgentConfig | None:
        return next((agent for agent in self.agents if agent.id == agent_id), None)


class BootstrapFile(_Strict):
    workspaces: tuple[WorkspaceConfig, ...] = ()

    def workspace(self, workspace_id: str) -> WorkspaceConfig | None:
        return next((ws for ws in self.workspaces if ws.id == workspace_id), None)


def load_bootstrap(path: Path) -> BootstrapFile:
    """Parse and validate a bootstrap file; any problem raises ``BootstrapConfigError``."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return BootstrapFile.model_validate(data or {})
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise BootstrapConfigError(str(error)) from None
