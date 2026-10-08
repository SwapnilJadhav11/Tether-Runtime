"""S1.4: RFC 8785 canonical JSON with Tether's number guards (ADR-0010, PD-28)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tether.core.canonical import MAX_DEPTH, MAX_SAFE_INTEGER, canonicalize
from tether.core.errors import (
    CanonicalizationError,
    FloatNotAllowedError,
    IntegerOutOfRangeError,
)

# --- RFC 8785 test vectors ------------------------------------------------------------------

# RFC 8785 §3.2.3: properties are sorted by their UTF-16 code units.
SORTING_INPUT = {
    "€": "Euro Sign",
    "\r": "Carriage Return",
    "דּ": "Hebrew Letter Dalet With Dagesh",
    "1": "One",
    "\U0001f600": "Emoji: Grinning Face",
    "\u0080": "Control",
    "ö": "Latin Small Letter O With Diaeresis",
}
SORTING_EXPECTED = (
    '{"\\r":"Carriage Return","1":"One","\u0080":"Control",'
    '"ö":"Latin Small Letter O With Diaeresis","€":"Euro Sign",'
    '"\U0001f600":"Emoji: Grinning Face","דּ":"Hebrew Letter Dalet With Dagesh"}'
).encode()

# RFC 8785 §3.2.2: whitespace removal, literals, number serialisation and string escaping.
FULL_INPUT = {
    "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
    "string": '€$\x0f\nA\'B"\\\\"/',
    "literals": [None, True, False],
}
FULL_EXPECTED = (
    r"""{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],"""
    r""""string":"€$\u000f\nA'B\"\\\\\"/"}"""
).encode()


def test_rfc8785_property_sorting_vector() -> None:
    assert canonicalize(SORTING_INPUT, allow_floats=False) == SORTING_EXPECTED


def test_rfc8785_full_vector_with_floats_allowed() -> None:
    assert canonicalize(FULL_INPUT, allow_floats=True) == FULL_EXPECTED


def test_rfc8785_nested_structures_and_empty_containers() -> None:
    value = {"b": [], "a": {"d": {}, "c": [1, "x", None]}}
    assert canonicalize(value, allow_floats=False) == b'{"a":{"c":[1,"x",null],"d":{}},"b":[]}'


# --- Integer range: ±(2^53 - 1) for every envelope ------------------------------------------


@pytest.mark.parametrize("allow_floats", [True, False])
@pytest.mark.parametrize("value", [MAX_SAFE_INTEGER, -MAX_SAFE_INTEGER, 0])
def test_integers_within_safe_range_are_accepted(value: int, allow_floats: bool) -> None:
    assert canonicalize({"n": value}, allow_floats=allow_floats) == f'{{"n":{value}}}'.encode()


@pytest.mark.parametrize("allow_floats", [True, False])
@pytest.mark.parametrize("value", [MAX_SAFE_INTEGER + 1, -MAX_SAFE_INTEGER - 1, 10**30])
def test_integers_beyond_safe_range_are_rejected(value: int, allow_floats: bool) -> None:
    with pytest.raises(IntegerOutOfRangeError):
        canonicalize({"nested": [{"n": value}]}, allow_floats=allow_floats)


def test_max_safe_integer_is_two_to_the_53_minus_one() -> None:
    assert MAX_SAFE_INTEGER == 2**53 - 1


def test_booleans_are_not_treated_as_integers() -> None:
    assert canonicalize([True, False], allow_floats=False) == b"[true,false]"


# --- Floats: rejected when not allowed (write envelopes) -----------------------------------


@pytest.mark.parametrize("value", [1.5, 1.0, 0.0, -2.25, float("inf"), float("nan")])
def test_floats_are_rejected_when_not_allowed(value: float) -> None:
    with pytest.raises(FloatNotAllowedError):
        canonicalize({"args": {"x": [value]}}, allow_floats=False)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_floats_are_rejected_even_when_floats_are_allowed(value: float) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize({"x": value}, allow_floats=True)


def test_guard_errors_do_not_echo_the_rejected_value() -> None:
    with pytest.raises(FloatNotAllowedError) as float_error:
        canonicalize({"amount": 123.456}, allow_floats=False)
    assert "123.456" not in str(float_error.value)

    big = 98765432109876543210
    with pytest.raises(IntegerOutOfRangeError) as int_error:
        canonicalize({"amount": big}, allow_floats=False)
    assert str(big) not in str(int_error.value)


# --- Other rejected input --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        {"x": (1, 2)},
        {"x": {1, 2}},
        {"x": b"bytes"},
        {"x": Decimal("1.5")},
        {1: "non-string key"},
        {"x": object()},
    ],
)
def test_unsupported_types_are_rejected(value: Any) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize(value, allow_floats=True)


def test_invalid_unicode_is_rejected_without_echo() -> None:
    with pytest.raises(CanonicalizationError) as exc:
        canonicalize({"x": "secret\ud800"}, allow_floats=False)
    assert "secret" not in str(exc.value)


# --- Properties ------------------------------------------------------------------------------

json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-MAX_SAFE_INTEGER, max_value=MAX_SAFE_INTEGER)
    | st.text()
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4)
    ),
    max_leaves=20,
)


@given(json_values)
def test_canonical_output_round_trips_to_an_equal_value(value: Any) -> None:
    assert json.loads(canonicalize(value, allow_floats=False)) == value


@given(st.dictionaries(st.text(), json_scalars, min_size=1, max_size=8))
def test_canonical_output_is_independent_of_key_insertion_order(value: dict[str, Any]) -> None:
    reversed_order = dict(reversed(list(value.items())))
    assert canonicalize(value, allow_floats=False) == canonicalize(
        reversed_order, allow_floats=False
    )


@given(st.lists(st.text(), min_size=3, max_size=3), st.lists(st.text(), min_size=3, max_size=3))
def test_array_encoding_is_unambiguous_at_element_boundaries(a: list[str], b: list[str]) -> None:
    """Distinct tuples never share an encoding: ["ab", "c"] and ["a", "bc"] cannot collide."""
    if a != b:
        assert canonicalize(a, allow_floats=False) != canonicalize(b, allow_floats=False)


# --- Security review: every failure is typed and never echoes input --------------------------


def _nested_lists(depth: int) -> list[Any]:
    root: list[Any] = []
    current = root
    for _ in range(depth - 1):
        child: list[Any] = []
        current.append(child)
        current = child
    return root


def test_lone_surrogate_in_a_key_is_rejected_without_echo() -> None:
    with pytest.raises(CanonicalizationError) as exc:
        canonicalize({"secret\ud800key": 1}, allow_floats=False)
    message = str(exc.value)
    assert "secret" not in message
    assert "\ud800" not in message
    assert "\ud800" not in message


def test_nesting_up_to_the_depth_limit_is_accepted() -> None:
    assert canonicalize(_nested_lists(MAX_DEPTH), allow_floats=False).startswith(b"[[")


@pytest.mark.parametrize("depth", [MAX_DEPTH + 1, 100_000])
def test_nesting_beyond_the_depth_limit_is_a_typed_error(depth: int) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize(_nested_lists(depth), allow_floats=False)


def test_self_referencing_list_is_a_typed_error() -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(CanonicalizationError):
        canonicalize(cyclic, allow_floats=False)


def test_self_referencing_dict_is_a_typed_error() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(CanonicalizationError):
        canonicalize(cyclic, allow_floats=False)


def test_depth_limit_is_a_small_explicit_constant() -> None:
    assert 16 <= MAX_DEPTH <= 128
