"""End-user JWT validation (ADR-0013, AD-03, PD-5).

Validation, in order, before anything else happens on a request:
- the header's ``alg`` is a supported asymmetric algorithm and ``kid`` names a key;
- the issuer is one of the token's workspace's platform-controlled issuers (``iss``), is allowed
  to issue for the route's audience, pins exactly this algorithm and holds this key ID;
- the signature verifies with that public key; ``aud`` is exactly the route's audience (the
  expected audience comes from the route, never from the token); ``exp`` is required and
  ``exp``/``nbf`` hold within 10 seconds of leeway; ``sub`` is a non-empty string; the
  ``workspace`` claim names that issuer's workspace; ``roles``, if present, is a list of strings.

Any failure raises ``InvalidCredentials``; callers answer ``401`` and log nothing about the token.
Only public keys are ever used here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from tether.identity.principal import Principal
from tether.storage import tables

# PD-5: asymmetric algorithms only; each issuer pins one of them. ``none`` and HMAC never.
ALGORITHMS = frozenset({"EdDSA", "RS256"})
LEEWAY_SECONDS = 10


class Audience(StrEnum):
    TASKS = "tether-tasks"
    APPROVALS = "tether-approvals"
    OPS = "tether-ops"


class InvalidCredentials(Exception):  # noqa: N818 - a rejection, mapped to 401
    """The credentials are missing or invalid. The message never contains them."""


@dataclass(frozen=True)
class IssuerRecord:
    workspace_id: str
    issuer_id: str
    iss: str
    audiences: frozenset[str]
    algorithm: str
    public_keys: Mapping[str, str]


IssuerLookup = Callable[[str, str], Awaitable[IssuerRecord | None]]


@lru_cache(maxsize=256)
def _public_key(pem: str) -> Any:
    return load_pem_public_key(pem.encode("ascii"))


async def validate_token(token: str, *, audience: Audience, lookup: IssuerLookup) -> Principal:
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise InvalidCredentials("malformed token") from None
    algorithm, kid = header.get("alg"), header.get("kid")
    workspace, iss = unverified.get("workspace"), unverified.get("iss")
    if algorithm not in ALGORITHMS or not isinstance(kid, str):
        raise InvalidCredentials("unsupported algorithm or missing key id")
    if not isinstance(workspace, str) or not workspace or not isinstance(iss, str):
        raise InvalidCredentials("missing workspace or issuer")
    # The unverified claims only choose the issuer record; everything is verified below.
    record = await lookup(workspace, iss)
    if (
        record is None
        or record.algorithm != algorithm
        or audience.value not in record.audiences
        or kid not in record.public_keys
    ):
        raise InvalidCredentials("unknown issuer, audience, algorithm or key")
    try:
        claims = jwt.decode(
            token,
            _public_key(record.public_keys[kid]),
            algorithms=[record.algorithm],
            audience=audience.value,
            issuer=record.iss,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except (jwt.PyJWTError, ValueError):
        raise InvalidCredentials("token rejected") from None
    subject, roles, token_id = claims.get("sub"), claims.get("roles", []), claims.get("jti")
    if claims.get("aud") not in (audience.value, [audience.value]):
        raise InvalidCredentials("token is for more than this audience")
    if not isinstance(subject, str) or not subject:
        raise InvalidCredentials("missing subject")
    if claims.get("workspace") != record.workspace_id:
        raise InvalidCredentials("workspace mismatch")
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise InvalidCredentials("malformed roles")
    return Principal(
        workspace_id=record.workspace_id,
        subject=subject,
        roles=tuple(roles),
        audience=audience.value,
        token_id=token_id if isinstance(token_id, str) else None,
    )


def database_issuers(engine: AsyncEngine) -> IssuerLookup:
    """Issuer records as applied from bootstrap (``tether.issuers``, ADR-0014)."""
    issuers = tables.issuers

    async def lookup(workspace_id: str, iss: str) -> IssuerRecord | None:
        statement = select(issuers).where(
            issuers.c.workspace_id == workspace_id, issuers.c.iss == iss
        )
        async with engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().first()
        if row is None:
            return None
        return IssuerRecord(
            workspace_id=row["workspace_id"],
            issuer_id=row["issuer_id"],
            iss=row["iss"],
            audiences=frozenset(row["audiences"]),
            algorithm=row["algorithm"],
            public_keys=dict(row["public_keys"]),
        )

    return lookup
