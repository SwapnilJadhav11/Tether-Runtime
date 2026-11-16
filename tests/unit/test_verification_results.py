"""W5.1, T-27 (unit half): probe result classification and evidence masking (ADR-0019 rules 7,
10; ADR-0022 rule 11).

Only a well-formed definitive result is definitive. An exception, a timeout, a result that is not
a ``VerificationResult`` and evidence that does not validate against the declared
``evidence_model`` are all ``inconclusive``. Evidence passes through the ADR-0017 masking choke
point; a ``Sensitive`` evidence field never leaves the helper unmasked.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from tether.queue import backoff
from tether.registry.masking import MASK
from tether.runtime.verification import ProbeAnswer, classify, run_probe
from tether.sdk import (
    Sensitive,
    VerificationResult,
    VerifyBackoff,
    VerifyContext,
    VerifyDeclaration,
)

CANARY = "canary-verification-secret"


class Args(BaseModel):
    key: str


class Evidence(BaseModel):
    found: bool
    token: Annotated[str, Sensitive]


class Other(BaseModel):
    unrelated: int


def declaration(probe: Any) -> VerifyDeclaration:
    return VerifyDeclaration(
        probe=probe, evidence_model=Evidence, required_permissions=("thing:read",)
    )


def answer(result: Any) -> ProbeAnswer:
    return classify(declaration(_never), result)


async def _never(args: Any, ctx: VerifyContext) -> VerificationResult:
    raise AssertionError("not called")


@pytest.mark.parametrize("kind", ["confirmed_applied", "confirmed_not_applied", "inconclusive"])
def test_t27_a_well_formed_result_keeps_its_kind_and_masked_evidence(kind: Any) -> None:
    found = answer(VerificationResult(kind, Evidence(found=True, token=CANARY)))
    assert found == ProbeAnswer(kind, {"found": True, "token": MASK}, None)
    assert CANARY not in repr(found)


def test_t27_evidence_of_another_model_is_inconclusive_without_evidence() -> None:
    found = answer(VerificationResult("confirmed_applied", Other(unrelated=1)))
    assert found == ProbeAnswer("inconclusive", None, "evidence_invalid")


def test_t27_something_other_than_a_result_is_inconclusive() -> None:
    assert answer({"kind": "confirmed_applied"}) == ProbeAnswer(
        "inconclusive", None, "result_invalid"
    )


def _run(probe: Any, timeout: float = 1.0) -> ProbeAnswer:
    return asyncio.run(
        run_probe(declaration(probe), Args(key="k"), VerifyContext("tth_key"), timeout)
    )


def test_t27_a_probe_error_is_inconclusive_and_names_only_the_error_type() -> None:
    async def probe(args: Any, ctx: VerifyContext) -> VerificationResult:
        raise RuntimeError(CANARY)

    found = _run(probe)
    assert found == ProbeAnswer("inconclusive", None, "probe_error:RuntimeError")
    assert CANARY not in repr(found)


def test_t27_a_probe_timeout_is_inconclusive() -> None:
    async def probe(args: Any, ctx: VerifyContext) -> VerificationResult:
        await asyncio.sleep(5)
        raise AssertionError("not reached")

    assert _run(probe, timeout=0.05) == ProbeAnswer("inconclusive", None, "probe_timeout")


def test_t27_the_probe_receives_exactly_the_arguments_and_the_key() -> None:
    seen: list[tuple[Any, Any]] = []

    async def probe(args: Any, ctx: VerifyContext) -> VerificationResult:
        seen.append((args, ctx))
        return VerificationResult("confirmed_not_applied", Evidence(found=False, token=CANARY))

    found = _run(probe)
    assert found == ProbeAnswer("confirmed_not_applied", {"found": False, "token": MASK}, None)
    assert seen == [(Args(key="k"), VerifyContext("tth_key"))]


def test_t27_verification_backoff_is_full_jitter_under_the_declared_ceiling() -> None:
    declared = VerifyBackoff(base_seconds=1.0, multiplier=2.0, cap_seconds=30.0)
    assert backoff.verify_backoff_seconds(declared, 1, rng=lambda: 1.0) == 1.0
    assert backoff.verify_backoff_seconds(declared, 3, rng=lambda: 1.0) == 4.0
    assert backoff.verify_backoff_seconds(declared, 9, rng=lambda: 1.0) == 30.0
    assert backoff.verify_backoff_seconds(declared, 9, rng=lambda: 0.5) == 15.0
    assert backoff.verify_backoff_seconds(declared, 2, rng=lambda: 0.0) == 0.0
