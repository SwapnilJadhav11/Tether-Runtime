"""Call envelope v1, keyed call hash and idempotency keys (ADR-0008, ADR-0010; AD-09, AD-11).

- ``call_hash = HMAC-SHA-256(K_bind, JCS(envelope))`` as lowercase hex. ``K_bind`` is the run's
  pinned call-binding key; callers obtain it from the key ring (``tether.secrets``).
- ``idempotency_key = "tth_" + base32(SHA-256(JCS([run_id, step_ordinal, call_hash])))``,
  lowercase and unpadded. Computed once when the ledger row is created, then stored.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tether.core.canonical import MAX_SAFE_INTEGER, canonicalize, check_json
from tether.core.errors import InvalidCallError, InvalidKeyMaterialError
from tether.core.ids import AgentId, RunId, WorkspaceId, is_canonical_run_id

ENVELOPE_VERSION = 1
ENVELOPE_FIELDS = (
    "v",
    "workspace_id",
    "run_id",
    "agent_id",
    "tool",
    "tool_version",
    "arguments",
    "preconditions",
)
BINDING_KEY_LENGTH = 32
IDEMPOTENCY_KEY_PREFIX = "tth_"

_CALL_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CallEnvelope:
    """What a call hash binds. The model's call ID and rationale are deliberately not fields.

    ``arguments`` are the validated input model in JSON mode, unmasked. ``preconditions`` maps
    each declared precondition field name to its value; it is a subset of ``arguments`` (PD-28).
    """

    workspace_id: WorkspaceId
    run_id: RunId
    agent_id: AgentId
    tool: str
    tool_version: str
    arguments: Mapping[str, Any]
    preconditions: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not is_canonical_run_id(self.run_id):
            raise InvalidCallError("run_id must be a canonical lowercase UUID")
        for name in ("workspace_id", "agent_id", "tool", "tool_version"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise InvalidCallError(f"{name} must be a non-empty string")
        for name in ("arguments", "preconditions"):
            if not isinstance(getattr(self, name), Mapping):
                raise InvalidCallError(f"{name} must be a mapping")
            # Validate before copying: cycles and excessive depth become typed errors.
            check_json(dict(getattr(self, name)), allow_floats=True)
        # Copy so later mutation of the caller's mappings cannot change what is bound.
        object.__setattr__(self, "arguments", copy.deepcopy(dict(self.arguments)))
        object.__setattr__(self, "preconditions", copy.deepcopy(dict(self.preconditions)))
        for name, value in self.preconditions.items():
            if name not in self.arguments:
                raise InvalidCallError("every precondition must name an argument field")
            if not _same_json(value, self.arguments[name]):
                raise InvalidCallError("a precondition value must equal its argument value")

    def to_json(self) -> dict[str, Any]:
        return {
            "v": ENVELOPE_VERSION,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "arguments": dict(self.arguments),
            "preconditions": dict(self.preconditions),
        }


def canonical_envelope(envelope: CallEnvelope, *, write: bool) -> bytes:
    """RFC 8785 bytes of the envelope. Write-call envelopes reject floats (PD-28)."""
    return canonicalize(envelope.to_json(), allow_floats=not write)


def call_hash(envelope: CallEnvelope, *, key: bytes, write: bool) -> str:
    """HMAC-SHA-256 of the canonical envelope under the run's call-binding key, lowercase hex."""
    if type(key) is not bytes or len(key) != BINDING_KEY_LENGTH:
        raise InvalidKeyMaterialError("the call-binding key must be exactly 32 bytes")
    return hmac.new(key, canonical_envelope(envelope, write=write), hashlib.sha256).hexdigest()


def call_hashes_equal(a: object, b: object) -> bool:
    """Compare two call hashes in constant time. Malformed hashes never compare equal."""
    if not (isinstance(a, str) and isinstance(b, str)):
        return False
    if not (_CALL_HASH.fullmatch(a) and _CALL_HASH.fullmatch(b)):
        return False
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


def idempotency_key(run_id: RunId, step_ordinal: int, call_hash_hex: str) -> str:
    """The idempotency key for one logical execution: ``(run_id, step_ordinal, call_hash)``."""
    if not is_canonical_run_id(run_id):
        raise InvalidCallError("run_id must be a canonical lowercase UUID")
    if (
        isinstance(step_ordinal, bool)
        or not isinstance(step_ordinal, int)
        or not 0 <= step_ordinal <= MAX_SAFE_INTEGER
    ):
        raise InvalidCallError("step_ordinal must be an integer in [0, 2^53 - 1]")
    if not isinstance(call_hash_hex, str) or not _CALL_HASH.fullmatch(call_hash_hex):
        raise InvalidCallError("call_hash must be 64 lowercase hex characters")
    material = canonicalize([run_id, step_ordinal, call_hash_hex], allow_floats=False)
    digest = hashlib.sha256(material).digest()
    return IDEMPOTENCY_KEY_PREFIX + base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def _same_json(a: Any, b: Any) -> bool:
    """JSON equality: unlike ``==``, ``True`` != ``1`` and ``1`` != ``1.0`` here."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same_json(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same_json(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)
