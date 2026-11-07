"""MVP M6: the masking choke point (ADR-0017)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel

from tether.registry.masking import MASK, mask
from tether.sdk import Sensitive


class Inner(BaseModel):
    ssn: Annotated[str, Sensitive]
    label: str


class Outer(BaseModel):
    name: str
    card_number: Annotated[str, Sensitive]
    inner: Inner
    items: list[Inner]
    optional: Inner | None = None


def test_sensitive_fields_are_masked_recursively() -> None:
    value = Outer(
        name="svc",
        card_number="4111-1111",
        inner=Inner(ssn="t1", label="a"),
        items=[Inner(ssn="t2", label="b")],
        optional=Inner(ssn="t3", label="c"),
    )
    masked = mask(value)
    assert masked == {
        "name": "svc",
        "card_number": MASK,
        "inner": {"ssn": MASK, "label": "a"},
        "items": [{"ssn": MASK, "label": "b"}],
        "optional": {"ssn": MASK, "label": "c"},
    }
    assert "4111-1111" not in repr(masked)


def test_models_without_sensitive_fields_pass_through() -> None:
    class Plain(BaseModel):
        healthy: bool
        restarts: int

    assert mask(Plain(healthy=True, restarts=2)) == {"healthy": True, "restarts": 2}
