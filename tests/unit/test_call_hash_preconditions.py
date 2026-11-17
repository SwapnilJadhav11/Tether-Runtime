"""W3.2 / T-21 (unit level): preconditions in the call hash (ADR-0021 rules 5 and 16, PD-28).

The run graph's ``call_hash_for`` fills the existing envelope ``preconditions`` field with each
declared precondition name and its argument value. The envelope version is unchanged, so a tool
without preconditions keeps exactly the hash it had before W3.2.
"""

from __future__ import annotations

import base64
from typing import Any

from pydantic import BaseModel

from tether.core.hashing import ENVELOPE_VERSION, CallEnvelope, call_hash
from tether.core.ids import AgentId, RunId, WorkspaceId
from tether.runtime.graph import call_hash_for
from tether.sdk import ToolContext, ToolDescriptor
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyRing

KEY = bytes(range(32))
KEYS = KeyRing(
    EnvSecretsProvider(
        {"TETHER_BINDING_KEY_K1": base64.urlsafe_b64encode(KEY).decode().rstrip("=")}
    )
)
RUN = {
    "id": "8f14e45f-ceea-467e-a2b6-0e1d3f1c7a10",
    "workspace_id": "ws-a",
    "agent_id": "agent-1",
    "binding_key_id": "K1",
}


class ConfigIn(BaseModel):
    service: str
    expected_version: int


class Out(BaseModel):
    ok: bool


async def _handler(args: Any, ctx: ToolContext) -> Out:
    return Out(ok=True)


def tool(preconditions: tuple[str, ...] = ()) -> ToolDescriptor:
    return ToolDescriptor(
        name="set_config",
        version="1.0.0",
        description="probe",
        input_model=ConfigIn,
        output_model=Out,
        effect="write",
        idempotency="supported",
        handler=_handler,
        risk="low",
        required_permissions=("config:write",),
        preconditions=preconditions,
    )


def expected(arguments: dict[str, Any], preconditions: dict[str, Any]) -> str:
    envelope = CallEnvelope(
        workspace_id=WorkspaceId(RUN["workspace_id"]),
        run_id=RunId(RUN["id"]),
        agent_id=AgentId(RUN["agent_id"]),
        tool="set_config",
        tool_version="1.0.0",
        arguments=arguments,
        preconditions=preconditions,
    )
    return call_hash(envelope, key=KEY, write=True)


ARGS = {"service": "api", "expected_version": 7}


def test_w3_2_the_envelope_version_is_unchanged() -> None:
    assert ENVELOPE_VERSION == 1


def test_t21_a_tool_without_preconditions_keeps_its_pre_w3_2_hash() -> None:
    hashed = call_hash_for(KEYS, RUN, tool(), ConfigIn(**ARGS))
    assert hashed == expected(ARGS, {})


def test_t21_declared_preconditions_and_their_values_enter_the_hash() -> None:
    hashed = call_hash_for(KEYS, RUN, tool(("expected_version",)), ConfigIn(**ARGS))
    assert hashed == expected(ARGS, {"expected_version": 7})
    assert hashed != expected(ARGS, {})


def test_t21_a_changed_expected_value_changes_the_hash() -> None:
    declared = tool(("expected_version",))
    seven = call_hash_for(KEYS, RUN, declared, ConfigIn(service="api", expected_version=7))
    eight = call_hash_for(KEYS, RUN, declared, ConfigIn(service="api", expected_version=8))
    assert seven != eight
