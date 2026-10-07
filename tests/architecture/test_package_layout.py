"""The core package layout matches section 4 of docs/architecture/tether-v1-architecture.md.

Adding a top-level core module is an architecture change: update section 4 (and, if it affects
the workload boundary, the import-linter contracts) before adding it here.
"""

from __future__ import annotations

from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[2] / "src" / "tether"

ARCHITECTURE_SUBPACKAGES = frozenset(
    {
        "sdk",
        "core",
        "identity",
        "authz",
        "policy",
        "registry",
        "providers",
        "runtime",
        "ledger",
        "approvals",
        "reconciliation",
        "audit",
        "queue",
        "secrets",
        "storage",
        "observability",
        "api",
        "worker",
        "cli",
        "testing",
    }
)
ARCHITECTURE_MODULES = frozenset({"settings"})


def test_core_subpackages_match_architecture() -> None:
    present = {p.name for p in CORE_ROOT.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert present == ARCHITECTURE_SUBPACKAGES, (
        f"missing={sorted(ARCHITECTURE_SUBPACKAGES - present)} "
        f"unexpected={sorted(present - ARCHITECTURE_SUBPACKAGES)}"
    )
    for name in ARCHITECTURE_SUBPACKAGES:
        assert (CORE_ROOT / name / "__init__.py").is_file(), f"tether.{name} is not a package"


def test_core_top_level_modules_match_architecture() -> None:
    present = {p.stem for p in CORE_ROOT.glob("*.py") if p.name != "__init__.py"}
    assert present == ARCHITECTURE_MODULES


def test_core_is_marked_typed() -> None:
    assert (CORE_ROOT / "py.typed").is_file(), "tether must ship a PEP 561 py.typed marker"
