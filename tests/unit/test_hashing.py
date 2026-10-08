"""S1.4: envelope v1 → keyed call hash (ADR-0010, AD-11) and idempotency keys (ADR-0008, AD-09)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import subprocess
import sys
import uuid
from dataclasses import replace
from typing import Any

import pytest
import rfc8785
from hypothesis import given
from hypothesis import strategies as st

from tether.core.errors import (
    CanonicalizationError,
    FloatNotAllowedError,
    IntegerOutOfRangeError,
    InvalidCallError,
    InvalidKeyMaterialError,
)
from tether.core.hashing import (
    ENVELOPE_FIELDS,
    CallEnvelope,
    call_hash,
    call_hashes_equal,
    canonical_envelope,
    idempotency_key,
)
from tether.core.ids import AgentId, RunId, WorkspaceId, parse_run_id

KEY_A = bytes(range(32))
KEY_B = bytes(range(1, 33))
RUN_ID = parse_run_id("8F14E45F-CEEA-467E-A2B6-0E1D3F1C7A10")


def envelope(**changes: Any) -> CallEnvelope:
    base = CallEnvelope(
        workspace_id=WorkspaceId("ws-one"),
        run_id=RUN_ID,
        agent_id=AgentId("agent-one"),
        tool="set_value",
        tool_version="1.0.0",
        arguments={"target": "alpha", "value": 3, "expected_version": 7},
        preconditions={"expected_version": 7},
    )
    return replace(base, **changes)


def write_hash(env: CallEnvelope, key: bytes = KEY_A) -> str:
    return call_hash(env, key=key, write=True)


# --- run_id and envelope shape ---------------------------------------------------------------


def test_run_id_is_canonical_lowercase_uuid() -> None:
    assert RUN_ID == "8f14e45f-ceea-467e-a2b6-0e1d3f1c7a10"
    assert parse_run_id(uuid.UUID(RUN_ID)) == RUN_ID


def test_non_canonical_run_id_is_rejected_by_the_envelope() -> None:
    with pytest.raises(InvalidCallError):
        envelope(run_id=RunId("8F14E45F-CEEA-467E-A2B6-0E1D3F1C7A10"))


def test_envelope_v1_has_exactly_the_architecture_fields() -> None:
    assert set(ENVELOPE_FIELDS) == {
        "v",
        "workspace_id",
        "run_id",
        "agent_id",
        "tool",
        "tool_version",
        "arguments",
        "preconditions",
    }
    assert set(envelope().to_json()) == set(ENVELOPE_FIELDS)
    assert envelope().to_json()["v"] == 1


def test_call_hash_is_lowercase_hex_hmac_sha256_over_jcs_envelope() -> None:
    expected = hmac.new(KEY_A, rfc8785.dumps(envelope().to_json()), hashlib.sha256).hexdigest()
    assert write_hash(envelope()) == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)


# --- T-08 at unit level: what the hash binds -------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"tool": "set_other"},
        {"tool_version": "1.0.1"},
        {"arguments": {"target": "beta", "value": 3, "expected_version": 7}},
        {"arguments": {"target": "alpha", "value": 4, "expected_version": 7}},
        {"arguments": {"target": "alpha", "value": 3, "expected_version": 7, "extra": None}},
        {
            "arguments": {"target": "alpha", "value": 3, "expected_version": 8},
            "preconditions": {"expected_version": 8},
        },
        {"preconditions": {}},
        {"workspace_id": WorkspaceId("ws-two")},
        {"agent_id": AgentId("agent-two")},
        {"run_id": parse_run_id("0b9e1c3a-0000-4000-8000-000000000001")},
    ],
)
def test_t08_any_bound_change_changes_the_hash(changes: dict[str, Any]) -> None:
    assert write_hash(envelope(**changes)) != write_hash(envelope())


def test_t08_call_id_and_rationale_cannot_enter_the_envelope() -> None:
    proposal = {"call_id": "call_123", "rationale": "the model says so"}
    with pytest.raises(TypeError):
        CallEnvelope(**proposal)  # type: ignore[call-arg]
    serialized = rfc8785.dumps(envelope().to_json())
    assert b"call_id" not in serialized
    assert b"rationale" not in serialized


def test_same_envelope_same_key_same_hash() -> None:
    assert write_hash(envelope()) == write_hash(envelope())


def test_different_binding_keys_give_different_hashes() -> None:
    assert write_hash(envelope(), KEY_A) != write_hash(envelope(), KEY_B)


@pytest.mark.parametrize("key", [b"", b"short", bytes(31), bytes(33)])
def test_binding_key_must_be_32_bytes(key: bytes) -> None:
    with pytest.raises(InvalidKeyMaterialError):
        write_hash(envelope(), key)


def test_same_inputs_give_the_same_hash_in_fresh_subprocesses() -> None:
    """No per-process state (hash seeds, dict order) leaks into hashes, so replays match."""
    results = set()
    for seed in ("0", "12345"):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from tether.core.hashing import CallEnvelope, call_hash\n"
                "from tether.core.ids import AgentId, WorkspaceId, parse_run_id\n"
                "env = CallEnvelope(workspace_id=WorkspaceId('ws-one'),"
                " run_id=parse_run_id('8f14e45f-ceea-467e-a2b6-0e1d3f1c7a10'),"
                " agent_id=AgentId('agent-one'), tool='set_value', tool_version='1.0.0',"
                " arguments={'target': 'alpha', 'value': 3, 'expected_version': 7},"
                " preconditions={'expected_version': 7})\n"
                "print(call_hash(env, key=bytes(range(32)), write=True))\n",
            ],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=True,
        )
        results.add(completed.stdout.strip())
    assert results == {write_hash(envelope())}


# --- Number guards on envelopes ---------------------------------------------------------------


def test_write_envelopes_reject_floats() -> None:
    env = envelope(arguments={"target": "alpha", "value": 3.5, "expected_version": 7})
    with pytest.raises(FloatNotAllowedError):
        call_hash(env, key=KEY_A, write=True)


def test_read_envelopes_accept_floats() -> None:
    env = envelope(arguments={"threshold": 0.5}, preconditions={})
    assert re.fullmatch(r"[0-9a-f]{64}", call_hash(env, key=KEY_A, write=False))


@pytest.mark.parametrize("write", [True, False])
def test_all_envelopes_enforce_the_safe_integer_range(write: bool) -> None:
    # Rejected no later than hashing; the envelope itself already refuses the value.
    with pytest.raises(IntegerOutOfRangeError):
        call_hash(envelope(arguments={"value": 2**53}, preconditions={}), key=KEY_A, write=write)


# --- Preconditions: a field-name → value subset of the arguments (PD-28) --------------------


def test_precondition_must_name_an_argument_field() -> None:
    with pytest.raises(InvalidCallError):
        envelope(preconditions={"not_an_argument": 7})


def test_precondition_value_must_match_the_argument() -> None:
    with pytest.raises(InvalidCallError):
        envelope(preconditions={"expected_version": 8})


# --- Idempotency keys (AD-09) -----------------------------------------------------------------

HASH = "a" * 64


def test_idempotency_key_matches_the_architecture_formula() -> None:
    digest = hashlib.sha256(rfc8785.dumps([RUN_ID, 3, HASH])).digest()
    expected = "tth_" + base64.b32encode(digest).decode().rstrip("=").lower()
    assert idempotency_key(RUN_ID, 3, HASH) == expected


def test_idempotency_key_format() -> None:
    assert re.fullmatch(r"tth_[a-z2-7]{52}", idempotency_key(RUN_ID, 0, HASH))


def test_idempotency_key_is_deterministic() -> None:
    assert idempotency_key(RUN_ID, 5, HASH) == idempotency_key(RUN_ID, 5, HASH)


@pytest.mark.parametrize(
    ("run_id", "ordinal", "hash_"),
    [
        (parse_run_id("0b9e1c3a-0000-4000-8000-000000000001"), 3, HASH),
        (RUN_ID, 4, HASH),
        (RUN_ID, 3, "b" * 64),
    ],
)
def test_idempotency_key_changes_with_each_component(
    run_id: RunId, ordinal: int, hash_: str
) -> None:
    assert idempotency_key(run_id, ordinal, hash_) != idempotency_key(RUN_ID, 3, HASH)


@pytest.mark.parametrize(
    ("ordinal", "hash_"),
    [
        (-1, HASH),
        (True, HASH),
        (2**53, HASH),
        (1, "A" * 64),
        (1, "a" * 63),
        (1, "g" * 64),
    ],
)
def test_idempotency_key_rejects_invalid_components(ordinal: Any, hash_: str) -> None:
    with pytest.raises(InvalidCallError):
        idempotency_key(RUN_ID, ordinal, hash_)


@given(
    st.tuples(st.uuids(), st.integers(0, 2**53 - 1), st.binary(min_size=32, max_size=32)),
    st.tuples(st.uuids(), st.integers(0, 2**53 - 1), st.binary(min_size=32, max_size=32)),
)
def test_distinct_executions_get_distinct_idempotency_keys(
    a: tuple[uuid.UUID, int, bytes], b: tuple[uuid.UUID, int, bytes]
) -> None:
    def key(t: tuple[uuid.UUID, int, bytes]) -> str:
        return idempotency_key(parse_run_id(t[0]), t[1], t[2].hex())

    assert (key(a) == key(b)) == (a == b)


# --- Security review: strict field and key types, constant-time comparison --------------------


@pytest.mark.parametrize("field", ["workspace_id", "agent_id", "tool", "tool_version"])
@pytest.mark.parametrize("value", ["", 5, ["a"], True, 1.5, None])
def test_string_fields_must_be_non_empty_strings(field: str, value: Any) -> None:
    with pytest.raises(InvalidCallError):
        envelope(**{field: value})


@pytest.mark.parametrize("key", [bytearray(range(32)), memoryview(bytes(range(32))), "k" * 32])
def test_binding_key_must_be_bytes(key: Any) -> None:
    with pytest.raises(InvalidKeyMaterialError):
        write_hash(envelope(), key)


def test_cyclic_arguments_are_a_typed_error() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(CanonicalizationError):
        envelope(arguments={"value": cyclic}, preconditions={})


def test_call_hashes_equal_compares_hashes() -> None:
    assert call_hashes_equal(HASH, "a" * 64)
    assert not call_hashes_equal(HASH, "b" * 64)
    assert not call_hashes_equal(HASH, "A" * 64)
    assert not call_hashes_equal(HASH, "a" * 63)


def test_call_hashes_equal_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, object]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert call_hashes_equal(HASH, "a" * 64)
    assert calls, "call_hashes_equal must use hmac.compare_digest"


# --- Known-answer tests: fixed values, computed independently with the standard library -------
# (json.dumps with sorted keys and compact separators equals RFC 8785 for this ASCII, integer-only
# envelope.) These catch any change in canonicalisation or hashing, including a library upgrade.

KAT_CANONICAL = (
    b'{"agent_id":"agent-one","arguments":{"expected_version":7,"target":"alpha","value":3},'
    b'"preconditions":{"expected_version":7},"run_id":"8f14e45f-ceea-467e-a2b6-0e1d3f1c7a10",'
    b'"tool":"set_value","tool_version":"1.0.0","v":1,"workspace_id":"ws-one"}'
)
KAT_CALL_HASH = "71f6321e5f6b71ef56c533a4ff6335c706fc3109c090af0bb22ff8622ec9ae12"
KAT_IDEMPOTENCY_KEY = "tth_ucy72stwjcqcpic6cnovgtnatelgy2agobe7sqbbsmhtebjynrhq"


def test_kat_canonical_envelope_bytes() -> None:
    assert canonical_envelope(envelope(), write=True) == KAT_CANONICAL


def test_kat_call_hash() -> None:
    assert call_hash(envelope(), key=bytes(range(32)), write=True) == KAT_CALL_HASH


def test_kat_idempotency_key() -> None:
    assert idempotency_key(RUN_ID, 3, KAT_CALL_HASH) == KAT_IDEMPOTENCY_KEY
