"""Flake gate: run a pytest target N times and report every failure.

Cross-platform, no shell. It acquires nothing itself: the caller holds the shared database lock
and has loaded the test environment (``uv run poe test-flake-gate`` does the latter).

    uv run poe test-flake-gate                       # chaos suite, 20 iterations
    uv run python scripts/flake_gate.py -n 2 -t tests/chaos/test_chaos_heartbeats.py

One line per iteration goes to stdout and a JSON summary to ``--summary``. The exit status is 1
when any iteration failed (including a pytest crash that produced no report), otherwise 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TARGETS = ("tests/chaos",)
DEFAULT_MARKER = "chaos"
DEFAULT_ITERATIONS = 20
DEFAULT_SUMMARY = ".pytest_cache/flake-gate-summary.json"  # git-ignored
# Reported when pytest exits non-zero without naming a failing test (crash, collection error,
# interrupted run): the iteration must still count as failed.
NO_REPORT_ID = "<pytest exited without a test report>"


@dataclass
class Iteration:
    index: int
    exit_code: int
    seconds: float
    tests: int
    failed_ids: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.failed_ids


def parse_junit(xml_text: str) -> tuple[int, list[str]]:
    """Return ``(test count, ids of failed or errored tests)`` from a JUnit XML report."""
    root = ET.fromstring(xml_text)  # noqa: S314 - the report pytest just wrote, not untrusted
    total = 0
    failed: list[str] = []
    for case in root.iter("testcase"):
        total += 1
        if case.find("failure") is not None or case.find("error") is not None:
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            failed.append(f"{classname}::{name}" if classname else name)
    return total, failed


def build_command(
    targets: Sequence[str], marker: str | None, junit_path: str, extra: Sequence[str]
) -> list[str]:
    command = [sys.executable, "-m", "pytest", *targets, "-q", f"--junitxml={junit_path}"]
    if marker:
        command += ["-m", marker]
    return [*command, *extra]


def summarize(iterations: Sequence[Iteration], command: Sequence[str]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for item in iterations:
        counts.update(set(item.failed_ids))
    durations = [round(item.seconds, 2) for item in iterations]
    return {
        "iterations": len(iterations),
        "failures": sum(1 for item in iterations if not item.passed),
        "failing_tests": dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))),
        "durations_seconds": durations,
        "total_seconds": round(sum(durations), 2),
        "command": list(command),
    }


def run_once(index: int, command_for: Callable[[str], list[str]], work_dir: Path) -> Iteration:
    junit = work_dir / f"junit-{index}.xml"
    started = time.monotonic()
    completed = subprocess.run(command_for(str(junit)), check=False)  # noqa: S603
    seconds = time.monotonic() - started
    tests, failed = 0, []
    if junit.exists():
        try:
            tests, failed = parse_junit(junit.read_text(encoding="utf-8"))
        except ET.ParseError:
            failed = [NO_REPORT_ID]
    if completed.returncode != 0 and not failed:
        failed = [NO_REPORT_ID]
    return Iteration(index, completed.returncode, seconds, tests, failed)


def emit(text: str) -> None:
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


def format_line(item: Iteration, total: int) -> str:
    verdict = "PASS" if item.passed else "FAIL"
    line = f"[flake-gate] {item.index}/{total} {verdict} {item.seconds:.1f}s tests={item.tests}"
    if item.failed_ids:
        line += " failed=" + ",".join(item.failed_ids)
    return line


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-n", "--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("-t", "--target", action="append", dest="targets", default=None)
    parser.add_argument(
        "-m", "--marker", default=DEFAULT_MARKER, help="pytest -m expression; '' for none"
    )
    parser.add_argument("--summary", default=DEFAULT_SUMMARY, help="JSON summary path")
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments (after --)")
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    targets = args.targets or list(DEFAULT_TARGETS)

    results: list[Iteration] = []
    with tempfile.TemporaryDirectory(prefix="flake-gate-") as tmp:

        def command_for(junit: str) -> list[str]:
            return build_command(targets, args.marker or None, junit, args.pytest_args)

        for index in range(1, args.iterations + 1):
            item = run_once(index, command_for, Path(tmp))
            results.append(item)
            emit(format_line(item, args.iterations))

    summary = summarize(results, command_for("<junit>"))
    Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    emit(
        f"[flake-gate] {summary['failures']} of {summary['iterations']} iterations failed;"
        f" summary: {args.summary}"
    )
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
