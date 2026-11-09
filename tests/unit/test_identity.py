"""T-18 (unit half): API keys (PD-20), JWT validation (ADR-0013, PD-5), bootstrap identity."""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from tether.identity import api_keys
from tether.identity.jwt import Audience, InvalidCredentials, IssuerRecord, validate_token
from tether.policy.config import BootstrapFile

WS = "ws-a"
ISS = "https://issuer.test/tasks"
JTI = "t-1"


def _ed25519() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


def _public_pem(key: Any) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _private_pem(key: Any) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


SIGNER = _ed25519()
OTHER = _ed25519()
RECORD = IssuerRecord(
    workspace_id=WS,
    issuer_id="tasks",
    iss=ISS,
    audiences=frozenset({"tether-tasks"}),
    algorithm="EdDSA",
    public_keys={"k1": _public_pem(SIGNER)},
)


async def _lookup(workspace_id: str, iss: str) -> IssuerRecord | None:
    return RECORD if (workspace_id, iss) == (WS, ISS) else None


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISS,
        "aud": "tether-tasks",
        "sub": "alice",
        "workspace": WS,
        "roles": ["operator"],
        "iat": now,
        "exp": now + 300,
        "jti": JTI,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(
    claims: dict[str, Any] | None = None,
    *,
    key: Any = SIGNER,
    kid: Any = "k1",
    algorithm: str = "EdDSA",
) -> str:
    headers = {} if kid is None else {"kid": kid}
    return jwt.encode(claims or _claims(), key, algorithm=algorithm, headers=headers)


async def _validate(token: str, audience: Audience = Audience.TASKS) -> Any:
    return await validate_token(token, audience=audience, lookup=_lookup)


# --- API keys (PD-20) ---------------------------------------------------------------------


def test_t18_an_api_key_has_the_documented_format_and_verifies_against_its_hash() -> None:
    issued = api_keys.generate("demo")
    assert issued.raw.startswith("tak_demo_")
    assert len(issued.salt) == 16
    assert len(issued.secret_hash) == 32
    assert api_keys.verify(issued.raw, salt=issued.salt, secret_hash=issued.secret_hash)
    parsed = api_keys.parse(issued.raw)
    assert parsed is not None
    assert parsed[0] == "demo"
    # The stored material is the salt and the hash, never the secret itself.
    assert parsed[1] not in issued.salt + issued.secret_hash


def test_t18_a_wrong_or_malformed_api_key_does_not_verify() -> None:
    issued = api_keys.generate("demo")
    other = api_keys.generate("demo")
    assert not api_keys.verify(other.raw, salt=issued.salt, secret_hash=issued.secret_hash)
    secret = issued.raw.removeprefix("tak_demo_")
    for raw in ("", "demo", "tak_demo", "tak__" + secret, "xyz_demo_" + secret, issued.raw + "A"):
        assert not api_keys.verify(raw, salt=issued.salt, secret_hash=issued.secret_hash)


# --- JWT validation -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t18_a_valid_token_yields_the_principal_from_its_claims_only() -> None:
    principal = await _validate(_token())
    assert principal.workspace_id == WS
    assert principal.subject == "alice"
    assert principal.roles == ("operator",)
    assert principal.audience == "tether-tasks"
    assert principal.token_id == JTI
    assert principal.requester == {"sub": "alice", "roles": ["operator"]}


INVALID: dict[str, Any] = {
    "bad signature": lambda: _token(key=OTHER),
    "wrong audience": lambda: _token(_claims(aud="tether-approvals")),
    "several audiences": lambda: _token(_claims(aud=["tether-tasks", "tether-ops"])),
    "wrong issuer": lambda: _token(_claims(iss="https://issuer.test/other")),
    "expired": lambda: _token(_claims(exp=int(time.time()) - 60)),
    "not yet valid": lambda: _token(_claims(nbf=int(time.time()) + 60)),
    "no expiry": lambda: _token(_claims(exp=None)),
    "no subject": lambda: _token(_claims(sub=None)),
    "empty subject": lambda: _token(_claims(sub="")),
    "no workspace": lambda: _token(_claims(workspace=None)),
    "other workspace": lambda: _token(_claims(workspace="ws-b")),
    "roles not a list": lambda: _token(_claims(roles="approver")),
    "no key id": lambda: _token(kid=None),
    "unknown key id": lambda: _token(kid="k2"),
    "alg none": lambda: jwt.encode(_claims(), None, algorithm="none", headers={"kid": "k1"}),
    "symmetric alg": lambda: jwt.encode(
        _claims(), "x" * 32, algorithm="HS256", headers={"kid": "k1"}
    ),
    "alg not pinned for the issuer": lambda: _token(
        key=rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256"
    ),
    "not a token": lambda: "not-a-jwt",
}


@pytest.mark.parametrize("case", INVALID)
@pytest.mark.asyncio
async def test_t18_an_invalid_token_is_rejected(case: str) -> None:
    with pytest.raises(InvalidCredentials):
        await _validate(INVALID[case]())


@pytest.mark.asyncio
async def test_t18_the_expected_audience_comes_from_the_route() -> None:
    """A valid tasks token is refused on an approvals or ops route (no cross-audience reuse)."""
    token = _token()
    for audience in (Audience.APPROVALS, Audience.OPS):
        with pytest.raises(InvalidCredentials):
            await _validate(token, audience)


@pytest.mark.asyncio
async def test_t18_an_issuer_signs_only_for_its_configured_audiences() -> None:
    """The tasks issuer's key cannot mint an approvals token, even one aimed at that route."""
    with pytest.raises(InvalidCredentials):
        await _validate(_token(_claims(aud="tether-approvals")), Audience.APPROVALS)


@pytest.mark.asyncio
async def test_t18_exp_and_nbf_allow_ten_seconds_of_clock_skew() -> None:
    now = int(time.time())
    await _validate(_token(_claims(exp=now - 5, nbf=now + 5)))


# --- bootstrap: public keys and hashes only -------------------------------------------------


def _bootstrap(issuer: dict[str, Any], api_key: dict[str, Any] | None = None) -> BootstrapFile:
    issued = api_keys.generate("demo")
    key = api_key or {
        "key_id": "demo",
        "salt": issued.salt.hex(),
        "secret_hash": issued.secret_hash.hex(),
        "agents": ["agent-1"],
    }
    return BootstrapFile.model_validate(
        {
            "workspaces": [
                {
                    "id": WS,
                    "agents": [{"id": "agent-1", "workload": "w"}],
                    "issuers": [issuer],
                    "api_keys": [key],
                }
            ]
        }
    )


def _issuer(**overrides: Any) -> dict[str, Any]:
    issuer: dict[str, Any] = {
        "id": "tasks",
        "iss": ISS,
        "audiences": ["tether-tasks"],
        "algorithm": "EdDSA",
        "public_keys": {"k1": _public_pem(SIGNER)},
    }
    issuer.update(overrides)
    return issuer


def test_bootstrap_accepts_issuer_public_keys_and_api_key_hashes() -> None:
    [workspace] = _bootstrap(_issuer()).workspaces
    assert workspace.issuers[0].audiences == ("tether-tasks",)
    assert workspace.api_keys[0].agents == ("agent-1",)


@pytest.mark.parametrize(
    "issuer",
    [
        _issuer(public_keys={"k1": _private_pem(SIGNER)}),
        _issuer(public_keys={"k1": "not a key"}),
        _issuer(public_keys={}),
        _issuer(algorithm="RS256"),  # an Ed25519 key cannot verify RS256
        _issuer(algorithm="HS256"),
        _issuer(audiences=["tether-admin"]),
        _issuer(audiences=[]),
    ],
)
def test_bootstrap_rejects_private_keys_and_inconsistent_issuers(issuer: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic's ValidationError is a ValueError
        _bootstrap(issuer)


@pytest.mark.parametrize(
    "api_key",
    [
        {"key_id": "demo", "salt": "00" * 16, "secret_hash": "00" * 31, "agents": ["agent-1"]},
        {"key_id": "demo", "salt": "00" * 15, "secret_hash": "00" * 32, "agents": ["agent-1"]},
        {"key_id": "demo", "salt": "00" * 16, "secret_hash": "00" * 32, "agents": ["agent-9"]},
        {"key_id": "demo", "raw": "tak_demo_" + uuid.uuid4().hex, "agents": ["agent-1"]},
    ],
)
def test_bootstrap_rejects_raw_keys_bad_hashes_and_unknown_agents(api_key: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic's ValidationError is a ValueError
        _bootstrap(_issuer(), api_key)


def test_identity_settings_do_not_change_the_policy_version() -> None:
    plain = BootstrapFile.model_validate(
        {"workspaces": [{"id": WS, "agents": [{"id": "agent-1", "workload": "w"}]}]}
    )
    assert _bootstrap(_issuer()).workspaces[0].policy_version == plain.workspaces[0].policy_version
