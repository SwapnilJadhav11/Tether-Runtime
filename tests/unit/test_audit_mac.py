"""W2.4 (unit half): the version-2 audit MAC record and its timestamp representation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tether.audit.chain import MAC_VERSIONS, canonical_created_at, mac_record, mac_record_v2

RUN = uuid.UUID("8f14e45f-ceea-467e-a2b6-0e1d3f1c7a10")
FIELDS = {
    "workspace_id": "ws-a",
    "run_id": RUN,
    "seq": 3,
    "dedupe_key": "decision:0",
    "event_type": "POLICY_DECISION",
    "actor": None,
    "tool": "probe",
    "payload": {"decision": "allow"},
    "policy_version": "v",
    "prev_mac": "a" * 64,
}


def test_created_at_is_utc_with_six_fractional_digits_and_z() -> None:
    instant = datetime(2026, 11, 14, 9, 5, 7, 1200, tzinfo=UTC)
    assert canonical_created_at(instant) == "2026-11-14T09:05:07.001200Z"
    whole_second = datetime(2026, 11, 14, 9, 5, 7, tzinfo=UTC)
    assert canonical_created_at(whole_second) == "2026-11-14T09:05:07.000000Z"


def test_the_same_instant_in_another_offset_gives_the_same_text() -> None:
    instant = datetime(2026, 11, 14, 9, 5, 7, 1200, tzinfo=UTC)
    shifted = instant.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert canonical_created_at(shifted) == canonical_created_at(instant)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        canonical_created_at(datetime(2026, 11, 14, 9, 5, 7))


def test_the_v1_record_is_unchanged() -> None:
    assert set(mac_record(**FIELDS)) == {
        "workspace_id",
        "run_id",
        "seq",
        "dedupe_key",
        "event_type",
        "actor",
        "tool",
        "payload",
        "policy_version",
        "prev_mac",
    }


def test_the_v2_record_adds_only_the_version_and_the_time() -> None:
    instant = datetime(2026, 11, 14, 9, 5, 7, 1200, tzinfo=UTC)
    v2 = mac_record_v2(**FIELDS, created_at=instant)
    assert v2 == {
        **mac_record(**FIELDS),
        "mac_version": 2,
        "created_at": "2026-11-14T09:05:07.001200Z",
    }


def test_the_supported_versions_are_exactly_1_and_2() -> None:
    assert frozenset({1, 2}) == MAC_VERSIONS
