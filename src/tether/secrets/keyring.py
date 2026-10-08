"""Audit and call-binding key families (PD-21, ADR-0010).

Two independent families of 32-byte random keys, each with key IDs and one active ID:

    TETHER_AUDIT_ACTIVE_KEY_ID     TETHER_AUDIT_KEY_<ID>     (base64url)
    TETHER_BINDING_ACTIVE_KEY_ID   TETHER_BINDING_KEY_<ID>   (base64url)

Key IDs are 1-32 characters of A-Z and 0-9: environment variable names are case-insensitive on
Windows, so lowercase IDs could silently alias. A missing key raises ``KeyUnavailableError`` so
callers fail closed. Key material must be the canonical base64url encoding of exactly 32 bytes
(unpadded, or with its single ``=``); any other spelling of the same bytes is rejected.

Follow-up (deferred from the S1.4 security review, owner-approved): a startup/configuration
check that no audit key shares material with any binding key (ADR-0010, invariant 34) belongs
to the configuration/startup step, not to this module.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from tether.core.errors import InvalidKeyMaterialError, KeyUnavailableError, SecretNotFoundError
from tether.secrets.provider import SecretsProvider, SecretValue

KEY_LENGTH = 32

_KEY_ID = re.compile(r"[A-Z0-9]{1,32}")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")


class KeyFamily(StrEnum):
    AUDIT = "audit"
    BINDING = "binding"


@dataclass(frozen=True)
class KeyMaterial:
    family: KeyFamily
    key_id: str
    secret: SecretValue[bytes]


class KeyRing:
    __slots__ = ("_provider",)

    def __init__(self, provider: SecretsProvider) -> None:
        self._provider = provider

    def __repr__(self) -> str:
        return "KeyRing(<redacted>)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("KeyRing cannot be pickled")

    def __getstate__(self) -> NoReturn:
        raise TypeError("KeyRing does not expose its state")

    def active_key_id(self, family: KeyFamily) -> str:
        family = KeyFamily(family)
        name = f"TETHER_{family.upper()}_ACTIVE_KEY_ID"
        try:
            key_id = self._provider.get(name).reveal()
        except SecretNotFoundError:
            raise KeyUnavailableError(f"no active {family} key ID is configured") from None
        if not _KEY_ID.fullmatch(key_id):
            raise KeyUnavailableError(f"the active {family} key ID is malformed")
        return key_id

    def key(self, family: KeyFamily, key_id: str) -> KeyMaterial:
        family = KeyFamily(family)
        if not _KEY_ID.fullmatch(key_id):
            raise KeyUnavailableError(f"malformed {family} key ID")
        try:
            encoded = self._provider.get(f"TETHER_{family.upper()}_KEY_{key_id}").reveal()
        except SecretNotFoundError:
            raise KeyUnavailableError(f"{family} key {key_id} is not available") from None
        return KeyMaterial(family, key_id, SecretValue(_decode(encoded, family, key_id)))

    def active_key(self, family: KeyFamily) -> KeyMaterial:
        return self.key(family, self.active_key_id(family))


def _decode(encoded: str, family: KeyFamily, key_id: str) -> bytes:
    problem = f"{family} key {key_id} must be 32 bytes of base64url"
    if not _BASE64URL.fullmatch(encoded):
        raise InvalidKeyMaterialError(problem)
    unpadded = encoded.rstrip("=")
    try:
        raw = base64.urlsafe_b64decode(unpadded + "=" * (-len(unpadded) % 4))
    except (binascii.Error, ValueError):
        raise InvalidKeyMaterialError(problem) from None
    if len(raw) != KEY_LENGTH:
        raise InvalidKeyMaterialError(problem)
    canonical = base64.urlsafe_b64encode(raw).decode("ascii")
    if encoded not in (canonical, canonical.rstrip("=")):
        raise InvalidKeyMaterialError(problem)
    return raw
