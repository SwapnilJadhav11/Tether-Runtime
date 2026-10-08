"""S1.4: SecretValue masking, env SecretsProvider and key ring (ADR-0010, ADR-0017, PD-21)."""

from __future__ import annotations

import base64
import logging
import pickle
from typing import Any

import pytest

from tether.core.errors import (
    InvalidKeyMaterialError,
    KeyUnavailableError,
    SecretNotFoundError,
)
from tether.core.hashing import CallEnvelope, call_hash
from tether.core.ids import AgentId, WorkspaceId, parse_run_id
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyFamily, KeyRing
from tether.secrets.provider import SecretValue

SENSITIVE_TEXT = "s3cret-Value-XYZ"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# --- T-16: SecretValue never renders its value -------------------------------------------------


def test_t16_secret_value_is_masked_in_repr_str_and_format() -> None:
    value = SecretValue(SENSITIVE_TEXT)
    rendered = [repr(value), str(value), f"{value}", f"{value!r}", format(value, "")]
    assert all(SENSITIVE_TEXT not in text for text in rendered)
    assert value.reveal() == SENSITIVE_TEXT


def test_t16_secret_value_is_masked_in_log_output(caplog: pytest.LogCaptureFixture) -> None:
    value = SecretValue(SENSITIVE_TEXT)
    logger = logging.getLogger("tether.test.secrets")
    with caplog.at_level(logging.DEBUG, logger="tether.test.secrets"):
        logger.info("plain %s repr %r", value, value)
        logger.info({"nested": [value]})
        try:
            raise RuntimeError(value)
        except RuntimeError:
            logger.exception("failed with %s", value)
    assert SENSITIVE_TEXT not in caplog.text
    assert "failed with" in caplog.text


def test_t16_secret_value_cannot_be_pickled() -> None:
    with pytest.raises(TypeError):
        pickle.dumps(SecretValue(SENSITIVE_TEXT))


def test_secret_value_masks_bytes_too() -> None:
    raw = b"\x01\x02secret-bytes"
    value = SecretValue(raw)
    assert "secret-bytes" not in repr(value)
    assert value.reveal() == raw


# --- EnvSecretsProvider ------------------------------------------------------------------------


def test_env_provider_returns_secret_values() -> None:
    provider = EnvSecretsProvider({"TETHER_SOME_SECRET": SENSITIVE_TEXT})
    secret = provider.get("TETHER_SOME_SECRET")
    assert isinstance(secret, SecretValue)
    assert secret.reveal() == SENSITIVE_TEXT


@pytest.mark.parametrize("environ", [{}, {"TETHER_SOME_SECRET": ""}])
def test_env_provider_missing_or_empty_secret_raises(environ: dict[str, str]) -> None:
    with pytest.raises(SecretNotFoundError):
        EnvSecretsProvider(environ).get("TETHER_SOME_SECRET")


def test_env_provider_snapshots_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TETHER_SOME_SECRET", "first")
    provider = EnvSecretsProvider()
    monkeypatch.setenv("TETHER_SOME_SECRET", "second")
    assert provider.get("TETHER_SOME_SECRET").reveal() == "first"


# --- Key ring: two independent families (PD-21) -----------------------------------------------

AUDIT_K1 = bytes([1] * 32)
BINDING_K1 = bytes([2] * 32)
BINDING_K2 = bytes([3] * 32)


def keyring(**extra: str) -> KeyRing:
    environ = {
        "TETHER_AUDIT_ACTIVE_KEY_ID": "K1",
        "TETHER_AUDIT_KEY_K1": b64url(AUDIT_K1),
        "TETHER_BINDING_ACTIVE_KEY_ID": "K2",
        "TETHER_BINDING_KEY_K1": b64url(BINDING_K1),
        "TETHER_BINDING_KEY_K2": b64url(BINDING_K2),
        **extra,
    }
    return KeyRing(EnvSecretsProvider(environ))


def test_active_key_ids_are_resolved_per_family() -> None:
    ring = keyring()
    assert ring.active_key_id(KeyFamily.AUDIT) == "K1"
    assert ring.active_key_id(KeyFamily.BINDING) == "K2"


def test_key_material_is_decoded_base64url_32_bytes() -> None:
    material = keyring().key(KeyFamily.BINDING, "K1")
    assert material.family is KeyFamily.BINDING
    assert material.key_id == "K1"
    assert material.secret.reveal() == BINDING_K1


def test_padded_base64url_is_accepted() -> None:
    padded = base64.urlsafe_b64encode(BINDING_K1).decode()
    ring = keyring(TETHER_BINDING_KEY_K3=padded)
    assert ring.key(KeyFamily.BINDING, "K3").secret.reveal() == BINDING_K1


def test_families_are_independent() -> None:
    ring = keyring()
    # K1 exists in both families with different material; neither family reads the other.
    assert ring.key(KeyFamily.AUDIT, "K1").secret.reveal() == AUDIT_K1
    assert ring.key(KeyFamily.BINDING, "K1").secret.reveal() == BINDING_K1
    with pytest.raises(KeyUnavailableError):
        ring.key(KeyFamily.AUDIT, "K2")


def test_active_key_returns_the_active_material() -> None:
    material = keyring().active_key(KeyFamily.BINDING)
    assert material.key_id == "K2"
    assert material.secret.reveal() == BINDING_K2


def test_key_material_repr_does_not_reveal_the_key() -> None:
    material = keyring().key(KeyFamily.BINDING, "K1")
    encoded = b64url(BINDING_K1)
    assert encoded not in repr(material)
    assert repr(BINDING_K1) not in repr(material)


def test_missing_pinned_key_raises_key_unavailable() -> None:
    with pytest.raises(KeyUnavailableError):
        keyring().key(KeyFamily.BINDING, "K9")


def test_missing_active_key_id_raises_key_unavailable() -> None:
    ring = KeyRing(EnvSecretsProvider({"TETHER_BINDING_KEY_K1": b64url(BINDING_K1)}))
    with pytest.raises(KeyUnavailableError):
        ring.active_key_id(KeyFamily.BINDING)


@pytest.mark.parametrize("key_id", ["", "k1", "K-1", "K_1", "A" * 33, "../K1"])
def test_malformed_key_ids_are_unavailable(key_id: str) -> None:
    with pytest.raises(KeyUnavailableError):
        keyring().key(KeyFamily.BINDING, key_id)


@pytest.mark.parametrize(
    "encoded",
    [
        b64url(bytes(31)),
        b64url(bytes(33)),
        "not base64 at all!",
        base64.b64encode(bytes([0xFB] * 32)).decode(),  # standard alphabet ('+', '/')
    ],
)
def test_invalid_key_material_is_rejected_without_echo(encoded: str) -> None:
    ring = keyring(TETHER_BINDING_KEY_K3=encoded)
    with pytest.raises(InvalidKeyMaterialError) as exc:
        ring.key(KeyFamily.BINDING, "K3")
    assert encoded not in str(exc.value)


def test_different_binding_key_ids_give_different_call_hashes() -> None:
    ring = keyring()
    env = CallEnvelope(
        workspace_id=WorkspaceId("ws-one"),
        run_id=parse_run_id("8f14e45f-ceea-467e-a2b6-0e1d3f1c7a10"),
        agent_id=AgentId("agent-one"),
        tool="set_value",
        tool_version="1.0.0",
        arguments={"value": 1},
        preconditions={},
    )
    k1 = ring.key(KeyFamily.BINDING, "K1").secret.reveal()
    k2 = ring.key(KeyFamily.BINDING, "K2").secret.reveal()
    assert call_hash(env, key=k1, write=True) != call_hash(env, key=k2, write=True)


# --- Security review: no plaintext through state, pickling, repr or the env snapshot ---------

_B64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def test_secret_value_getstate_does_not_expose_the_value() -> None:
    with pytest.raises(TypeError):
        SecretValue(SENSITIVE_TEXT).__getstate__()


def test_env_provider_keeps_only_tether_variables() -> None:
    provider = EnvSecretsProvider({"TETHER_SOME_SECRET": "kept", "DB_PASSWORD": "unrelated"})
    assert provider.get("TETHER_SOME_SECRET").reveal() == "kept"
    with pytest.raises(SecretNotFoundError):
        provider.get("DB_PASSWORD")


def test_env_provider_and_key_ring_reprs_reveal_nothing() -> None:
    provider = EnvSecretsProvider({"TETHER_SOME_SECRET": SENSITIVE_TEXT})
    ring = KeyRing(provider)
    for text in (repr(provider), str(provider), repr(ring), str(ring)):
        assert SENSITIVE_TEXT not in text
        assert "TETHER_SOME_SECRET" not in text


@pytest.mark.parametrize("make", [lambda p: p, KeyRing])
def test_env_provider_and_key_ring_refuse_pickling(make: Any) -> None:
    provider = EnvSecretsProvider({"TETHER_SOME_SECRET": SENSITIVE_TEXT})
    with pytest.raises(TypeError):
        pickle.dumps(make(provider))


def _alias_with_flipped_unused_bits(raw: bytes) -> str:
    """A different base64url string that lenient decoders map to the same 32 bytes."""
    canonical = b64url(raw)
    last = _B64URL_ALPHABET.index(canonical[-1])
    alias = canonical[:-1] + _B64URL_ALPHABET[last ^ 0b01]
    assert alias != canonical
    assert base64.urlsafe_b64decode(alias + "=") == raw
    return alias


@pytest.mark.parametrize(
    "encoded",
    [
        _alias_with_flipped_unused_bits(BINDING_K1),
        b64url(BINDING_K1) + "==",
        " " + b64url(BINDING_K1),
        b64url(BINDING_K1) + "\n",
    ],
)
def test_non_canonical_base64url_is_rejected(encoded: str) -> None:
    ring = keyring(TETHER_BINDING_KEY_K3=encoded)
    with pytest.raises(InvalidKeyMaterialError):
        ring.key(KeyFamily.BINDING, "K3")


def test_key_family_strings_are_normalised_through_key_family() -> None:
    ring = keyring()
    material = ring.key("binding", "K1")  # type: ignore[arg-type]
    assert material.family is KeyFamily.BINDING
    assert ring.active_key_id("audit") == "K1"  # type: ignore[arg-type]


@pytest.mark.parametrize("family", ["BINDING", "other", ""])
def test_unknown_key_families_are_rejected(family: str) -> None:
    with pytest.raises(ValueError, match="KeyFamily"):
        keyring().key(family, "K1")  # type: ignore[arg-type]
