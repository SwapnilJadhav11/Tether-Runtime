"""Bootstrap configuration file: workspaces, agents, identity and declarative policy.

ADR-0014, ADR-0009, ADR-0013. Policy comes only from this file (invariant 11). Each workspace's
policy has a content hash, ``policy_version`` = SHA-256 of the RFC 8785 canonical JSON of its
``policy`` section. ``tether bootstrap apply`` records and activates that version; api and worker
processes load the same file and only decide under the version that is active in the database.

Identity config holds no secrets: issuers carry public verification keys only (private key
material is rejected), and API keys carry only a salt and a SHA-256 hash (PD-20).
"""

from __future__ import annotations

import hashlib
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


class ReconciliationPolicy(_Strict):
    """Who may resolve an ``indeterminate`` ledger entry (ADR-0007). Empty means nobody."""

    # A reconciler needs one of these roles and must not be the run's requester.
    reconciler_roles: tuple[str, ...] = ()


class PolicyConfig(_Strict):
    rules: tuple[PolicyRule, ...] = ()
    reconciliation: ReconciliationPolicy = ReconciliationPolicy()


class AgentConfig(_Strict):
    id: str = NonEmpty
    workload: str = NonEmpty


AudienceName = Literal["tether-tasks", "tether-approvals", "tether-ops"]
_KEY_TYPES = {"EdDSA": Ed25519PublicKey, "RS256": RSAPublicKey}


class IssuerConfig(_Strict):
    """A platform-controlled token issuer for one workspace (ADR-0013, AD-03)."""

    id: str = NonEmpty
    iss: str = NonEmpty
    # The audiences this issuer may sign for; a token's audience must also match its route.
    audiences: tuple[AudienceName, ...] = Field(min_length=1)
    # Pinned per issuer (PD-5): EdDSA (Ed25519) by default, RS256 allowed, nothing else.
    algorithm: Literal["EdDSA", "RS256"] = "EdDSA"
    # Key ID → PEM public key. Tokens name their key in the ``kid`` header.
    public_keys: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _public_keys_only(self) -> IssuerConfig:
        for kid, pem in self.public_keys.items():
            if not kid or "PRIVATE" in pem.upper():
                raise ValueError(f"issuer {self.id}: only public keys belong in bootstrap config")
            try:
                key = load_pem_public_key(pem.encode("ascii"))
            except (ValueError, TypeError, UnicodeEncodeError):
                raise ValueError(f"issuer {self.id}: key {kid} is not a PEM public key") from None
            if not isinstance(key, _KEY_TYPES[self.algorithm]):
                raise ValueError(f"issuer {self.id}: key {kid} does not match {self.algorithm}")
        return self


class ApiKeyConfig(_Strict):
    """A workspace API key: its ID, salt and SHA-256 hash only, never the raw key (PD-20)."""

    key_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    salt: str = Field(pattern=r"^[0-9a-f]{32}$")
    secret_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The agents this key may run (the agent is resolved server-side from this set).
    agents: tuple[str, ...] = ()


class WorkspaceConfig(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, ignored_types=(cached_property,))

    id: str = NonEmpty
    agents: tuple[AgentConfig, ...] = ()
    issuers: tuple[IssuerConfig, ...] = ()
    api_keys: tuple[ApiKeyConfig, ...] = ()
    policy: PolicyConfig = PolicyConfig()

    @model_validator(mode="after")
    def _identity_is_consistent(self) -> WorkspaceConfig:
        agent_ids = {agent.id for agent in self.agents}
        for key in self.api_keys:
            unknown = set(key.agents) - agent_ids
            if unknown:
                raise ValueError(f"api key {key.key_id}: unknown agent(s) {sorted(unknown)}")
        for values, what in (
            ([issuer.id for issuer in self.issuers], "issuer id"),
            ([issuer.iss for issuer in self.issuers], "issuer iss"),
            ([key.key_id for key in self.api_keys], "api key id"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"workspace {self.id}: duplicate {what}")
        return self

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
