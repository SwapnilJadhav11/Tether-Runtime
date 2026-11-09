"""Workspace API keys (ADR-0013, PD-20).

A raw key is ``tak_<key_id>_<base32 of 32 random bytes>``. Only ``key_id``, a 16-byte salt and
SHA-256(salt || secret) are stored (bootstrap config and the ``api_keys`` table); the raw key is
shown once at issue and never stored or logged. Verification compares in constant time. A key maps
to exactly one workspace and a set of allowed agents (``api_key_agents``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.storage import tables

PREFIX = "tak_"
SALT_BYTES = 16
SECRET_BYTES = 32
# No underscore in key IDs, so ``tak_<key_id>_<secret>`` parses one way only.
KEY_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_SECRET_TEXT = re.compile(r"[A-Z2-7]{52}")
_DUMMY_SALT = bytes(SALT_BYTES)


@dataclass(frozen=True)
class IssuedKey:
    """A freshly issued key: ``raw`` goes to the caller once; the rest goes into bootstrap."""

    key_id: str
    raw: str = field(repr=False)
    salt: bytes = field(repr=False)
    secret_hash: bytes = field(repr=False)


@dataclass(frozen=True)
class CallerApp:
    """The calling application behind a valid API key: its workspace and allowed agents."""

    key_id: str
    workspace_id: str
    allowed_agents: tuple[str, ...]


def secret_hash(salt: bytes, secret: bytes) -> bytes:
    return hashlib.sha256(salt + secret).digest()


def generate(key_id: str) -> IssuedKey:
    if not KEY_ID.fullmatch(key_id):
        raise ValueError("key IDs are lowercase letters, digits and dashes")
    secret = secrets.token_bytes(SECRET_BYTES)
    salt = secrets.token_bytes(SALT_BYTES)
    text = base64.b32encode(secret).decode("ascii").rstrip("=")
    return IssuedKey(
        key_id=key_id,
        raw=f"{PREFIX}{key_id}_{text}",
        salt=salt,
        secret_hash=secret_hash(salt, secret),
    )


def parse(raw: str) -> tuple[str, bytes] | None:
    """``(key_id, secret)`` of a well-formed raw key, else ``None``."""
    if not raw.startswith(PREFIX):
        return None
    key_id, separator, text = raw[len(PREFIX) :].rpartition("_")
    if not separator or not KEY_ID.fullmatch(key_id) or not _SECRET_TEXT.fullmatch(text):
        return None
    try:
        secret = base64.b32decode(text + "====")
    except binascii.Error:
        return None
    return (key_id, secret) if len(secret) == SECRET_BYTES else None


def verify(raw: str, *, salt: bytes, secret_hash: bytes) -> bool:
    parsed = parse(raw)
    if parsed is None:
        return False
    return hmac.compare_digest(_hash(salt, parsed[1]), secret_hash)


def _hash(salt: bytes, secret: bytes) -> bytes:
    return secret_hash(salt, secret)


async def authenticate(engine: AsyncEngine, raw: str) -> CallerApp | None:
    """The caller app for a valid raw key, or ``None``. Never logs or stores ``raw``."""
    parsed = parse(raw)
    if parsed is None:
        return None
    key_id, secret = parsed
    keys, bindings = tables.api_keys, tables.api_key_agents
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(keys.c.workspace_id, keys.c.salt, keys.c.secret_hash).where(
                    keys.c.key_id == key_id
                )
            )
        ).first()
        if row is None:
            # Same work as a known key, so timing does not reveal which key IDs exist.
            hmac.compare_digest(_hash(_DUMMY_SALT, secret), bytes(32))
            return None
        if not hmac.compare_digest(_hash(bytes(row.salt), secret), bytes(row.secret_hash)):
            return None
        agents = (
            await connection.execute(
                select(bindings.c.agent_id)
                .where(bindings.c.key_id == key_id, bindings.c.workspace_id == row.workspace_id)
                .order_by(bindings.c.agent_id)
            )
        ).scalars()
        return CallerApp(
            key_id=key_id, workspace_id=str(row.workspace_id), allowed_agents=tuple(agents)
        )
