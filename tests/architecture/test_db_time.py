"""Invariant 29: database time comes from clock_timestamp().

``now()`` and ``CURRENT_TIMESTAMP`` return the transaction start time, so they are forbidden
anywhere under ``src/tether`` (SQL, SQLAlchemy expressions, migrations, comments and strings).
Python-side ``datetime.now(...)`` is a different API and is not matched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parents[2] / "src" / "tether"

FORBIDDEN = {
    "now()": re.compile(r"(?<![\w.])now\s*\(\s*\)", re.IGNORECASE),
    "func.now": re.compile(r"\bfunc\s*\.\s*now\b", re.IGNORECASE),
    "CURRENT_TIMESTAMP": re.compile(r"\bcurrent_timestamp\b", re.IGNORECASE),
}


def scan(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            violations.extend(
                f"{path}:{lineno}: {name}"
                for name, pattern in FORBIDDEN.items()
                if pattern.search(line)
            )
    return violations


def test_core_uses_clock_timestamp_only() -> None:
    assert CORE_ROOT.is_dir()
    assert scan(CORE_ROOT) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "SELECT now()",
        "WHERE expires_at < NOW( )",
        "created_at timestamptz DEFAULT CURRENT_TIMESTAMP",
        "server_default=sa.func.now()",
        "x = func.current_timestamp()",
        "default=current_timestamp",
    ],
)
def test_scanner_flags_forbidden_time_functions(tmp_path: Path, snippet: str) -> None:
    (tmp_path / "probe.py").write_text(f"{snippet}\n", encoding="utf-8")
    assert scan(tmp_path)


@pytest.mark.parametrize(
    "snippet",
    [
        "DEFAULT clock_timestamp()",
        "stamp = datetime.now(UTC)",
        "known_now()",
        "snow()",
    ],
)
def test_scanner_allows_clock_timestamp_and_python_time(tmp_path: Path, snippet: str) -> None:
    (tmp_path / "probe.py").write_text(f"{snippet}\n", encoding="utf-8")
    assert scan(tmp_path) == []
