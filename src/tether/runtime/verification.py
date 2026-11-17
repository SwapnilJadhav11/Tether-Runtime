"""One verification probe and the classification of its answer (ADR-0019 rules 7 and 10, ADR-0022).

The probe is the tool version's declared read-back: it receives the original call's validated
arguments and a ``VerifyContext`` with the entry's idempotency key, and nothing else. Only a
well-formed definitive result is definitive. An exception, a timeout (the tool's
``timeout_seconds``), a result that is not a ``VerificationResult`` and evidence that does not
validate against the declared ``evidence_model`` are ``inconclusive``: truth is never inferred
without evidence. Evidence passes through the masking choke point (ADR-0017) here, before anything
persists, logs, audits or shows it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from tether.registry.masking import mask
from tether.sdk import VerificationKind, VerificationResult, VerifyContext, VerifyDeclaration

# ``resolved_by`` and the audit actor of a verified resolution (ADR-0019 rule 10).
VERIFIED_BY = "system:verification"
DEFINITIVE = frozenset({"confirmed_applied", "confirmed_not_applied"})


@dataclass(frozen=True)
class ProbeAnswer:
    kind: VerificationKind
    # Masked evidence, or ``None`` when the probe gave none that validates.
    evidence: dict[str, Any] | None
    # Why the answer is inconclusive when the probe did not say so itself (bounded vocabulary).
    reason: str | None

    @property
    def definitive(self) -> bool:
        return self.kind in DEFINITIVE


def classify(declaration: VerifyDeclaration, result: object) -> ProbeAnswer:
    """The probe's return value as a masked answer; anything malformed is inconclusive."""
    if not isinstance(result, VerificationResult):
        return ProbeAnswer("inconclusive", None, "result_invalid")
    try:
        evidence = declaration.evidence_model.model_validate(result.evidence.model_dump())
    except ValidationError:
        return ProbeAnswer("inconclusive", None, "evidence_invalid")
    return ProbeAnswer(result.kind, mask(evidence), None)


async def run_probe(
    declaration: VerifyDeclaration,
    arguments: BaseModel,
    context: VerifyContext,
    timeout_seconds: float,
) -> ProbeAnswer:
    """Call the declared read-back once, bounded by ``timeout_seconds``. Never raises except on
    cancellation (a lost lease), which the worker treats as its own death."""
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await declaration.probe(arguments, context)
    except TimeoutError:
        return ProbeAnswer("inconclusive", None, "probe_timeout")
    except Exception as error:
        # The error's type only: its message may carry target data (ADR-0017).
        return ProbeAnswer("inconclusive", None, f"probe_error:{type(error).__name__}")
    return classify(declaration, result)
