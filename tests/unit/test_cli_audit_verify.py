"""P-9 (unit half): the output and exit codes of ``tether audit verify``.

The database read is replaced by canned results; the real chain is covered by
``tests/integration/test_audit_verify_cli.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from typer.testing import CliRunner

from tether.audit.chain import ChainVerification
from tether.cli import audit as audit_cli
from tether.cli.main import app
from tether.core.errors import KeyUnavailableError

runner = CliRunner()
RUN = "8f14e45f-ceea-467e-a2b6-0e1d3f1c7a10"
SECRET_MARKERS = ("SUPER-SECRET-PAYLOAD", "postgresql://", "hunter2")


def report(**overrides: Any) -> ChainVerification:
    values: dict[str, Any] = {
        "valid": True,
        "event_count": 3,
        "last_seq": 3,
        "mac_versions": (1, 2),
        "failed_seq": None,
        "reason": None,
        "head_seq": 3,
        "head_consistent": True,
    }
    return ChainVerification(**{**values, **overrides})


def returning(result: ChainVerification) -> Any:
    async def load(workspace: str, run_id: uuid.UUID) -> ChainVerification:
        return result

    return load


def raising(error: BaseException) -> Any:
    async def load(workspace: str, run_id: uuid.UUID) -> ChainVerification:
        raise error

    return load


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TETHER_DATABASE_URL", "postgresql://unused:unused@localhost:1/unused")


def verify(*extra: str) -> Any:
    return runner.invoke(app, ["audit", "verify", "--workspace", "ws-a", "--run", RUN, *extra])


def test_a_valid_chain_exits_zero_and_reports_the_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_cli, "_load_report", returning(report()))
    result = verify()
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "VALID"
    for expected in (
        "workspace: ws-a",
        f"run: {RUN}",
        "events: 3",
        "last_seq: 3",
        "head: seq=3 consistent",
        "mac_versions: 1,2",
    ):
        assert expected in lines


def test_an_invalid_chain_exits_one_with_the_first_failing_event_and_a_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = report(valid=False, reason="mac_mismatch", failed_seq=2, head_consistent=None)
    monkeypatch.setattr(audit_cli, "_load_report", returning(bad))
    result = verify()
    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert lines[0] == "INVALID"
    assert "reason: mac_mismatch" in lines
    assert "failed_seq: 2" in lines
    assert "workspace: ws-a" in lines


def test_a_head_that_disagrees_with_the_events_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = report(valid=False, reason="head_mismatch", failed_seq=None, head_consistent=False)
    monkeypatch.setattr(audit_cli, "_load_report", returning(bad))
    result = verify()
    assert result.exit_code == 1
    assert "reason: head_mismatch" in result.output.splitlines()


def test_an_unknown_run_or_workspace_exits_four_without_saying_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = report(valid=True, event_count=0, last_seq=None, mac_versions=(), head_seq=None)
    monkeypatch.setattr(audit_cli, "_load_report", returning(empty))
    result = verify()
    assert result.exit_code == 4
    assert result.output.splitlines()[0] == "NOT_FOUND"


def test_an_unavailable_audit_key_is_an_operational_failure_not_an_invalid_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        audit_cli, "_load_report", raising(KeyUnavailableError("audit key K1 is not available"))
    )
    result = verify()
    assert result.exit_code == 3
    assert "INVALID" not in result.output


def test_a_database_failure_exits_three_and_prints_no_connection_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("could not connect to postgresql://app:hunter2@db:5432/tether")
    monkeypatch.setattr(audit_cli, "_load_report", raising(error))
    result = verify()
    assert result.exit_code == 3
    assert "INVALID" not in result.output
    for marker in SECRET_MARKERS:
        assert marker not in result.output


def test_a_run_id_that_is_not_a_uuid_is_a_usage_error() -> None:
    result = runner.invoke(app, ["audit", "verify", "--workspace", "ws-a", "--run", "nope"])
    assert result.exit_code == 2


def test_the_workspace_is_required_and_never_guessed() -> None:
    result = runner.invoke(app, ["audit", "verify", "--run", RUN])
    assert result.exit_code == 2


def test_missing_configuration_exits_two_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TETHER_DATABASE_URL", raising=False)
    result = verify()
    assert result.exit_code == 2
    assert "TETHER_DATABASE_URL" in result.output


def test_the_audit_command_is_listed() -> None:
    result = runner.invoke(app, ["--help"])
    assert "audit" in result.output
