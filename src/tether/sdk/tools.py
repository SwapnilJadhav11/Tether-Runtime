"""Tool, agent and workload declarations: the types a workload hands to Tether (§7, ADR-0002).

The governed tool contract is ADR-0020 (risk, permissions) and ADR-0021 (preconditions, timeout,
``on_exhausted``, retained versions, the ``verify`` declaration).
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

Effect = Literal["read", "write"]
IdempotencySupport = Literal["supported", "unsupported"]
# Fixed tool metadata for policy (ADR-0020): a high-risk tool is never allowed automatically.
Risk = Literal["low", "high"]
# What happens when a read's outcome stays unknown after its send budget (ADR-0021 rules 7-9):
# the model gets a typed ``failed`` result, or the run ends ``failed`` (reads only).
OnExhausted = Literal["typed_failure", "fail_run"]
VerificationKind = Literal["confirmed_applied", "confirmed_not_applied", "inconclusive"]

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# A permission is ``resource:action``, lowercase, matched exactly (no wildcards, ADR-0020).
PERMISSION = re.compile(r"[a-z0-9][a-z0-9_.-]*:[a-z0-9][a-z0-9_.-]*")
# One send attempt may wait at most this long (ADR-0021 rule 6); the default is PD-17's 10 s.
MAX_TIMEOUT_SECONDS = 300.0
_ON_EXHAUSTED = ("typed_failure", "fail_run")
_VERIFICATION_KINDS = ("confirmed_applied", "confirmed_not_applied", "inconclusive")


def _check_permissions(owner: str, permissions: object) -> None:
    if not isinstance(permissions, tuple) or not permissions:
        raise ValueError(f"{owner}: at least one required permission is needed")
    for permission in permissions:
        if not isinstance(permission, str) or not PERMISSION.fullmatch(permission):
            raise ValueError(f"{owner}: permission {permission!r} is not resource:action")


def _positive_finite(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


class Sensitive:
    """Marker for fields that must never be persisted, logged or shown unmasked (ADR-0017).

    Use as ``Annotated[str, Sensitive]`` on tool input or output model fields.
    """


class ToolNotApplied(Exception):  # noqa: N818 - an outcome, not an error: the call did nothing
    """Raised by a tool to report, with certainty, that it did not apply its effect.

    This is the only positive evidence of ``not_applied`` after a send (ADR-0019 rule 3), such as
    the target rejecting a stale precondition atomically, having applied nothing (ADR-0021).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ToolContext:
    """What Tether passes to a tool call. Forward ``idempotency_key`` on every write."""

    idempotency_key: str


ToolHandler = Callable[[Any, ToolContext], Awaitable[BaseModel]]


# --- verification declaration (ADR-0019; declaration only, executed from W5.1) -----------------


@dataclass(frozen=True)
class VerifyContext:
    """What Tether will pass to a verification probe: the original write's idempotency key."""

    idempotency_key: str


@dataclass(frozen=True)
class VerificationResult:
    """A probe's answer: ``confirmed_applied``, ``confirmed_not_applied`` or ``inconclusive``,
    with evidence (an instance of the declaration's ``evidence_model``)."""

    kind: VerificationKind
    evidence: BaseModel

    def __post_init__(self) -> None:
        if self.kind not in _VERIFICATION_KINDS:
            raise ValueError(f"unknown verification result kind {self.kind!r}")
        if not isinstance(self.evidence, BaseModel):
            raise ValueError("verification result evidence must be a model instance")


VerifyHandler = Callable[[Any, VerifyContext], Awaitable[VerificationResult]]


@dataclass(frozen=True)
class VerifyBackoff:
    """Delay between verification probes: base * multiplier^n, capped, full jitter (ADR-0019)."""

    base_seconds: float = 1.0
    multiplier: float = 2.0
    cap_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not (_positive_finite(self.base_seconds) and _positive_finite(self.cap_seconds)):
            raise ValueError("verification backoff base and cap must be positive and finite")
        if self.cap_seconds < self.base_seconds:
            raise ValueError("verification backoff cap must not be below its base")
        if not _positive_finite(self.multiplier) or self.multiplier < 1:
            raise ValueError("verification backoff multiplier must be at least 1")


@dataclass(frozen=True)
class VerifyDeclaration:
    """A write tool's safe read-back (ADR-0019 rule 7, ADR-0021 rule 17).

    ``probe`` performs safe reads only: it must never re-issue the write or mutate the target.
    ``required_permissions`` are what the read-back needs. W3.2 validates and stores this
    declaration; nothing invokes a probe until W5.1.
    """

    probe: VerifyHandler
    evidence_model: type[BaseModel]
    required_permissions: tuple[str, ...]
    max_attempts: int = 3
    backoff: VerifyBackoff = field(default_factory=VerifyBackoff)

    def __post_init__(self) -> None:
        if not callable(self.probe):
            raise ValueError("verification probe must be callable")
        model = self.evidence_model
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise ValueError("verification evidence_model must be a pydantic model class")
        _check_permissions("verification", self.required_permissions)
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValueError("verification max_attempts must be at least 1")
        if not isinstance(self.backoff, VerifyBackoff):
            raise ValueError("verification backoff must be a VerifyBackoff")


# --- the tool descriptor -----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    effect: Effect
    handler: ToolHandler
    # Policy metadata (ADR-0020): ``low`` or ``high``, and the permissions every caller (the
    # requester and the agent) must hold. Both are required; risk is never derived from a call.
    risk: Risk
    required_permissions: tuple[str, ...]
    # Required for writes: whether the target honours the forwarded idempotency key.
    idempotency: IdempotencySupport | None = None
    # Send attempts per call (PD-14), shared by retries and redeliveries. Only reads and
    # idempotent writes are ever re-sent; a non-idempotent write is sent at most once.
    max_attempts: int = 3
    # One send attempt, in seconds (ADR-0021 rule 6). A timeout is always an unknown outcome.
    timeout_seconds: float = 10.0
    # Reads only may end the run when their outcome stays unknown (ADR-0021 rules 7-9).
    on_exhausted: OnExhausted = "typed_failure"
    # Writes only: required top-level input fields the target checks atomically with the write
    # (ADR-0021 rules 2-5). Their names and values are bound into the call hash.
    preconditions: tuple[str, ...] = ()
    # Writes only: the ADR-0019 read-back declaration.
    verify: VerifyDeclaration | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"tool {self.name}: max_attempts must be at least 1")
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"invalid tool name {self.name!r}")
        if not self.version or not self.description:
            raise ValueError(f"tool {self.name}: version and description are required")
        if self.effect not in ("read", "write"):
            raise ValueError(f"tool {self.name}: effect must be read or write")
        if self.effect == "write" and self.idempotency is None:
            raise ValueError(f"write tool {self.name} must declare its idempotency support")
        if self.risk not in ("low", "high"):
            raise ValueError(f"tool {self.name}: risk must be low or high")
        _check_permissions(f"tool {self.name}", self.required_permissions)
        if not _positive_finite(self.timeout_seconds) or self.timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"tool {self.name}: timeout_seconds must be in (0, {MAX_TIMEOUT_SECONDS:g}]"
            )
        if self.on_exhausted not in _ON_EXHAUSTED:
            raise ValueError(f"tool {self.name}: on_exhausted must be one of {_ON_EXHAUSTED}")
        if self.on_exhausted == "fail_run" and self.effect != "read":
            raise ValueError(f"tool {self.name}: fail_run is allowed on read tools only")
        self._check_preconditions()
        if self.verify is not None:
            if not isinstance(self.verify, VerifyDeclaration):
                raise ValueError(f"tool {self.name}: verify must be a VerifyDeclaration")
            if self.effect != "write":
                raise ValueError(f"tool {self.name}: verify is allowed on write tools only")

    def _check_preconditions(self) -> None:
        if not isinstance(self.preconditions, tuple):
            raise ValueError(f"tool {self.name}: preconditions must be a tuple of field names")
        if not self.preconditions:
            return
        if self.effect != "write":
            raise ValueError(f"tool {self.name}: preconditions are allowed on write tools only")
        if len(set(self.preconditions)) != len(self.preconditions):
            raise ValueError(f"tool {self.name}: duplicate precondition names")
        fields = self.input_model.model_fields
        for name in self.preconditions:
            info = fields.get(name) if isinstance(name, str) else None
            if info is None:
                raise ValueError(
                    f"tool {self.name}: precondition {name!r} is not a top-level input field"
                )
            if not info.is_required():
                raise ValueError(
                    f"tool {self.name}: precondition {name!r} must be required, with no default"
                )


@dataclass(frozen=True)
class AgentDefinition:
    system_prompt: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class Workload:
    name: str
    # Offered tools: the version a new model turn may call, one per name.
    tools: tuple[ToolDescriptor, ...]
    agent: AgentDefinition
    # Earlier versions kept resolvable for calls already pinned to them, never offered
    # (ADR-0011 rule 2, ADR-0021 rule 13).
    retained_tools: tuple[ToolDescriptor, ...] = ()

    def __post_init__(self) -> None:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"workload {self.name}: duplicate tool names")
        unknown = set(self.agent.tools) - set(names)
        if unknown:
            raise ValueError(
                f"workload {self.name}: agent offers unknown tool(s) {sorted(unknown)}"
            )
        pins = [(tool.name, tool.version) for tool in (*self.tools, *self.retained_tools)]
        if len(pins) != len(set(pins)):
            raise ValueError(
                f"workload {self.name}: a (tool, version) pair is declared more than once"
            )
