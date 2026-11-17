"""W3.2: the governed tool contract (ADR-0021, TDR-009, U-008 tool-contract half).

Registration accepts exactly the combinations ADR-0021 allows: preconditions name required
top-level input fields of a write; ``timeout_seconds`` is per send attempt, finite, in (0, 300];
``fail_run`` is for reads only; ``verify`` is a declaration on a write; retained versions are
unique per ``(name, version)``.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from tether.sdk import (
    AgentDefinition,
    Sensitive,
    ToolContext,
    ToolDescriptor,
    VerificationResult,
    VerifyBackoff,
    VerifyContext,
    VerifyDeclaration,
    Workload,
)


class ConfigIn(BaseModel):
    service: str
    expected_version: int
    note: str = ""
    secret: Annotated[str, Sensitive] = ""


class Out(BaseModel):
    ok: bool


class Evidence(BaseModel):
    found: bool


async def _handler(args: Any, ctx: ToolContext) -> Out:
    return Out(ok=True)


async def _probe(args: Any, ctx: VerifyContext) -> VerificationResult:
    return VerificationResult(kind="inconclusive", evidence=Evidence(found=False))


def tool(**overrides: Any) -> ToolDescriptor:
    values: dict[str, Any] = {
        "name": "set_config",
        "version": "1.0.0",
        "description": "probe",
        "input_model": ConfigIn,
        "output_model": Out,
        "effect": "write",
        "idempotency": "supported",
        "handler": _handler,
        "risk": "low",
        "required_permissions": ("config:write",),
        **overrides,
    }
    return ToolDescriptor(**values)


def read(**overrides: Any) -> ToolDescriptor:
    return tool(
        **{"name": "get_config", "effect": "read", "idempotency": None, **overrides},
    )


def verify(**overrides: Any) -> VerifyDeclaration:
    values: dict[str, Any] = {
        "probe": _probe,
        "evidence_model": Evidence,
        "required_permissions": ("config:read",),
        **overrides,
    }
    return VerifyDeclaration(**values)


# --- defaults ----------------------------------------------------------------------------------


def test_w3_2_defaults_keep_existing_descriptors_valid_and_unchanged() -> None:
    descriptor = tool()
    assert descriptor.max_attempts == 3
    assert descriptor.timeout_seconds == 10.0
    assert descriptor.on_exhausted == "typed_failure"
    assert descriptor.preconditions == ()
    assert descriptor.verify is None


# --- timeout -----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0.5, 1, 10.0, 300.0])
def test_w3_2_a_timeout_in_range_is_accepted(value: float) -> None:
    assert tool(timeout_seconds=value).timeout_seconds == value


@pytest.mark.parametrize("value", [0, 0.0, -1.0, 300.01, math.inf, math.nan, True, "10"])
def test_w3_2_a_timeout_outside_zero_to_300_seconds_is_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="timeout"):
        tool(timeout_seconds=value)


# --- on_exhausted ------------------------------------------------------------------------------


def test_t04_fail_run_is_accepted_on_a_read() -> None:
    assert read(on_exhausted="fail_run").on_exhausted == "fail_run"


def test_t04_fail_run_is_rejected_on_a_write() -> None:
    with pytest.raises(ValueError, match="fail_run"):
        tool(on_exhausted="fail_run")


def test_t04_an_unknown_exhaustion_behaviour_is_rejected() -> None:
    with pytest.raises(ValueError, match="on_exhausted"):
        read(on_exhausted="fail_call")


# --- preconditions -----------------------------------------------------------------------------


def test_t21_a_write_may_declare_required_top_level_fields_as_preconditions() -> None:
    assert tool(preconditions=("expected_version",)).preconditions == ("expected_version",)


def test_t21_preconditions_are_rejected_on_a_read() -> None:
    with pytest.raises(ValueError, match="precondition"):
        read(preconditions=("expected_version",))


@pytest.mark.parametrize(
    "names",
    [
        ("missing",),  # not an input field
        ("note",),  # has a default: the model could omit it
        ("secret",),  # has a default
        ("expected_version", "expected_version"),  # duplicate
        ("expected_version.value",),  # not top-level
    ],
)
def test_t21_preconditions_must_name_distinct_required_input_fields(
    names: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="precondition"):
        tool(preconditions=names)


def test_t21_preconditions_must_be_a_tuple_of_names() -> None:
    with pytest.raises(ValueError, match="precondition"):
        tool(preconditions=["expected_version"])


# --- verify (declaration only, ADR-0019) -------------------------------------------------------


def test_w3_2_a_write_may_declare_a_verification_with_adr_0019_defaults() -> None:
    declared = tool(verify=verify()).verify
    assert declared is not None
    assert declared.max_attempts == 3
    assert declared.backoff == VerifyBackoff(base_seconds=1.0, multiplier=2.0, cap_seconds=30.0)
    assert declared.required_permissions == ("config:read",)


def test_w3_2_verify_is_rejected_on_a_read() -> None:
    with pytest.raises(ValueError, match="verify"):
        read(verify=verify())


def test_w3_2_verify_must_be_a_verification_declaration() -> None:
    with pytest.raises(ValueError, match="verify"):
        tool(verify=_probe)


@pytest.mark.parametrize(
    "overrides",
    [
        {"required_permissions": ()},
        {"required_permissions": ("config",)},
        {"max_attempts": 0},
        {"evidence_model": dict},
        {"probe": "not callable"},
        {"backoff": (1.0, 2.0, 30.0)},
    ],
)
def test_w3_2_an_invalid_verification_declaration_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="verif"):
        verify(**overrides)


@pytest.mark.parametrize(
    "values",
    [
        {"base_seconds": 0.0},
        {"base_seconds": 10.0, "cap_seconds": 5.0},
        {"multiplier": 0.5},
        {"cap_seconds": math.inf},
    ],
)
def test_w3_2_an_invalid_verification_backoff_is_rejected(values: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="backoff"):
        VerifyBackoff(**values)


def test_w3_2_a_verification_result_has_a_closed_set_of_kinds() -> None:
    with pytest.raises(ValueError, match="verification result"):
        VerificationResult(kind="maybe", evidence=Evidence(found=True))  # type: ignore[arg-type]


# --- retained versions (ADR-0011 rule 2) -------------------------------------------------------


def _workload(*, tools: tuple[ToolDescriptor, ...], retained: tuple[ToolDescriptor, ...]) -> Any:
    return Workload(
        name="w",
        tools=tools,
        agent=AgentDefinition("p", tuple(t.name for t in tools)),
        retained_tools=retained,
    )


def test_w3_2_an_earlier_version_may_be_retained_without_being_offered() -> None:
    workload = _workload(tools=(tool(version="2.0.0"),), retained=(tool(version="1.0.0"),))
    assert [t.version for t in workload.retained_tools] == ["1.0.0"]


def test_w3_2_a_retained_version_equal_to_an_offered_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="version"):
        _workload(tools=(tool(),), retained=(tool(),))


def test_w3_2_a_retained_version_listed_twice_is_rejected() -> None:
    with pytest.raises(ValueError, match="version"):
        _workload(tools=(tool(version="3.0.0"),), retained=(tool(), tool()))
