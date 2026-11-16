"""Parsing and summary logic of ``scripts/flake_gate.py`` (no database, no real test suite)."""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "flake_gate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("flake_gate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["flake_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()

REPORT = """<?xml version="1.0"?>
<testsuites><testsuite tests="4">
  <testcase classname="tests.a" name="test_ok"/>
  <testcase classname="tests.a" name="test_fails"><failure message="boom"/></testcase>
  <testcase classname="tests.b" name="test_errors"><error message="setup"/></testcase>
  <testcase classname="tests.b" name="test_skipped"><skipped/></testcase>
</testsuite></testsuites>
"""


def test_parse_junit_counts_tests_and_names_failures_and_errors() -> None:
    total, failed = gate.parse_junit(REPORT)
    assert total == 4
    assert failed == ["tests.a::test_fails", "tests.b::test_errors"]


def test_summary_counts_a_failing_test_once_per_iteration() -> None:
    items = [
        gate.Iteration(1, 0, 1.0, 3),
        gate.Iteration(2, 1, 2.5, 3, ["t::flaky", "t::flaky", "t::other"]),
        gate.Iteration(3, 1, 0.5, 3, ["t::flaky"]),
    ]
    summary = gate.summarize(items, ["pytest"])
    assert summary["iterations"] == 3
    assert summary["failures"] == 2
    assert summary["failing_tests"] == {"t::flaky": 2, "t::other": 1}
    assert summary["durations_seconds"] == [1.0, 2.5, 0.5]
    assert summary["total_seconds"] == 4.0


def test_a_nonzero_exit_without_failed_ids_is_still_a_failed_iteration() -> None:
    assert not gate.Iteration(1, 2, 1.0, 0).passed
    assert gate.Iteration(1, 0, 1.0, 5).passed
    assert not gate.Iteration(1, 0, 1.0, 5, ["t::x"]).passed


def test_build_command_uses_the_running_interpreter_and_marker() -> None:
    command = gate.build_command(["tests/chaos"], "chaos", "j.xml", ["-x"])
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert "--junitxml=j.xml" in command
    assert command[command.index("chaos") - 1] == "-m"
    assert command[-1] == "-x"
    assert "chaos" not in gate.build_command(["x"], None, "j.xml", [])[3:]


def _fake_pytest(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_pytest.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def test_run_once_reads_the_report_and_survives_a_crash_without_one(tmp_path: Path) -> None:
    script = _fake_pytest(tmp_path, "import sys; sys.exit(1)")

    def failing(junit: str) -> list[str]:
        Path(junit).write_text(REPORT, encoding="utf-8")
        return [sys.executable, str(script)]

    item = gate.run_once(1, failing, tmp_path)
    assert (item.exit_code, item.tests, len(item.failed_ids)) == (1, 4, 2)

    crashed = gate.run_once(2, lambda _junit: [sys.executable, str(script)], tmp_path)
    assert crashed.failed_ids == [gate.NO_REPORT_ID]
    assert not crashed.passed


def test_main_writes_the_summary_and_exits_nonzero_on_any_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outcomes = iter([0, 1])

    def fake_build(
        targets: list[str], marker: str | None, junit: str, extra: list[str]
    ) -> list[str]:
        code = 0 if junit == "<junit>" else next(outcomes)  # the summary asks for a template
        return [sys.executable, "-c", f"import sys; sys.exit({code})"]

    monkeypatch.setattr(gate, "build_command", fake_build)
    summary_path = tmp_path / "out" / "summary.json"
    status = gate.main(["-n", "2", "--summary", str(summary_path)])
    assert status == 1
    written = json.loads(summary_path.read_text(encoding="utf-8"))
    assert (written["iterations"], written["failures"]) == (2, 1)
    assert written["failing_tests"] == {gate.NO_REPORT_ID: 1}
    out = capsys.readouterr().out.splitlines()
    verdicts = [line.split()[2] for line in out if line.startswith("[flake-gate] ") and "/" in line]
    assert verdicts[:2] == ["PASS", "FAIL"]
