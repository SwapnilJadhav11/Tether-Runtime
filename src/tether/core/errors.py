"""Typed errors for core primitives.

Messages never include the offending value: validation errors can reach audit, logs and model
context, and inputs may be sensitive (ADR-0017).
"""

from __future__ import annotations


class TetherError(Exception):
    """Base class for Tether's typed errors."""


class CanonicalizationError(TetherError, ValueError):
    """A value cannot be canonicalised with RFC 8785."""


class FloatNotAllowedError(CanonicalizationError):
    """A float appeared where floats are not allowed (write-call envelopes, PD-28)."""


class IntegerOutOfRangeError(CanonicalizationError):
    """An integer is outside ±(2^53 - 1), the range JSON numbers represent exactly."""


class InvalidCallError(TetherError, ValueError):
    """A call envelope or idempotency-key component is malformed."""


class InvalidKeyMaterialError(TetherError, ValueError):
    """Key material is not 32 bytes of valid base64url."""


class KeyUnavailableError(TetherError, LookupError):
    """A required key (or the active key ID) is missing; callers must fail closed (ADR-0010)."""


class SecretNotFoundError(TetherError, LookupError):
    """A named secret is not available from the SecretsProvider."""
