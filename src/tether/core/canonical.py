"""RFC 8785 (JCS) canonical JSON with Tether's number guards (ADR-0010, PD-28).

- Integers must be within ±(2^53 - 1) in every envelope: larger values lose precision in JSON.
- Floats are rejected when ``allow_floats`` is false (write-call envelopes). When allowed, they
  must be finite and are serialised as RFC 8785 specifies.
- Only JSON types are accepted: None, bool, int, float, str, list and dict with string keys.
- Strings and keys must be valid Unicode; containers may nest at most ``MAX_DEPTH`` levels
  (which also rejects cycles).

Every failure is a typed ``CanonicalizationError`` whose message never echoes the input.
"""

from __future__ import annotations

import math
from typing import Any

import rfc8785

from tether.core.errors import (
    CanonicalizationError,
    FloatNotAllowedError,
    IntegerOutOfRangeError,
)

MAX_SAFE_INTEGER = 2**53 - 1
MAX_DEPTH = 64


def canonicalize(value: Any, *, allow_floats: bool) -> bytes:
    """Return the RFC 8785 canonical UTF-8 encoding of ``value``."""
    check_json(value, allow_floats=allow_floats)
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, UnicodeError, RecursionError, TypeError, ValueError):
        raise CanonicalizationError("value cannot be canonicalised with RFC 8785") from None


def check_json(value: Any, *, allow_floats: bool) -> None:
    """Validate that ``value`` can be canonicalised, without serialising it."""
    try:
        _check(value, allow_floats=allow_floats, depth=0)
    except RecursionError:
        raise CanonicalizationError("value is nested too deeply") from None


def _check_text(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise CanonicalizationError("strings must be valid Unicode") from None


def _check(value: Any, *, allow_floats: bool, depth: int) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        _check_text(value)
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise IntegerOutOfRangeError("integer is outside ±(2^53 - 1)")
        return
    if isinstance(value, float):
        if not allow_floats:
            raise FloatNotAllowedError("floats are not allowed in write-call envelopes")
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite numbers cannot be canonicalised")
        return
    if isinstance(value, list | dict):
        if depth >= MAX_DEPTH:
            raise CanonicalizationError(f"value is nested more than {MAX_DEPTH} levels deep")
        if isinstance(value, list):
            for item in value:
                _check(item, allow_floats=allow_floats, depth=depth + 1)
            return
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("object keys must be strings")
            _check_text(key)
            _check(item, allow_floats=allow_floats, depth=depth + 1)
        return
    raise CanonicalizationError(f"unsupported type for canonical JSON: {type(value).__name__}")
