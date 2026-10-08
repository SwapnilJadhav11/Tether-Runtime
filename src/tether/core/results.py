"""Neutral tool-result outcomes recorded in provider-neutral history (AD-06, PD-18)."""

from __future__ import annotations

from enum import StrEnum


class CallOutcome(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    FAILED = "failed"
    DENIED = "denied"
    INVALID = "invalid"
    REJECTED = "rejected"
    EXPIRED = "expired"
    RECONCILED = "reconciled"
    SKIPPED = "skipped"
