"""T-14: Tether core never depends on, or names, workload-specific code (ADR-0002).

Three layers of protection:

1. import-linter forbidden contracts in ``pyproject.toml`` (run by ``poe contracts``).
   This module verifies the contracts exist, cover every core module, and actually fire.
2. A static scan of ``src/tether`` for references to the ``workloads`` package and for
   reference-workload domain terms (code, comments, docstrings, strings, non-Python files).
3. Negative self-tests proving both checkers fail when a violation is introduced.

The word "workloads" is enforced structurally (imports, module-path strings, file paths, import
text) rather than as a bare substring, because core must name the entry-point group
``tether.workloads`` (ADR-0002) and may describe workloads generically. A future core reader of
a bootstrap key literally named "workloads" will need an explicit, narrow allowance here.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "src" / "tether"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Reference-workload (incident remediation) vocabulary from the PRD. None of these may appear
# anywhere under src/tether: file contents (code, comments, docstrings, string literals,
# non-Python files) or file and directory names.
DOMAIN_TERMS: tuple[str, ...] = (
    "incident",
    "runbook",
    "get_service_health",
    "query_service_logs",
    "get_service_config",
    "lookup_runbook",
    "restart_service",
    "update_service_config",
    "demo_target",
    "demo-target",
    "ops-demo",
    "ops_demo",
    "on-call",
)


def _term_pattern(term: str) -> re.Pattern[str]:
    """Match ``term`` at the start of any word, in any case and separator style.

    Parts of a multi-part term may be joined by ``_``, ``-`` or nothing, so ``restart_service``
    also matches ``restart-service``, ``RestartService`` and ``restartService``. The term must
    begin a word: after a non-alphanumeric character (``x_incident``, ``SERVICE_INCIDENT``), at
    a camelCase hump (``ServiceIncident``, ``isOnCall``) or after an acronym
    (``HTTPIncident``). This flags realistic identifiers while not flagging words that merely
    contain a term mid-word (``coincident``, ``functionCall``, ``function_call``).
    """
    body = r"[-_]?".join(re.escape(part) for part in re.split(r"[-_]", term) if part)
    first, rest = body[0], body[1:]
    return re.compile(
        rf"(?i:(?<![a-z0-9]){body})"  # word start
        rf"|(?<=[a-z0-9]){first.upper()}(?i:{rest})"  # camelCase hump
        rf"|(?<=[A-Z]){first.upper()}(?=[a-z])(?i:{rest})"  # after an acronym
    )


_TERM_PATTERNS = {term: _term_pattern(term) for term in DOMAIN_TERMS}

# A reference to the top-level ``workloads`` package written as a module path or file path
# (``workloads.x``, ``workloads/x``, ``workloads\x``) or as import text (``import workloads``,
# e.g. inside an ``exec`` string or a non-Python file). A preceding ``.`` or word character is
# excluded so the entry-point group name ``tether.workloads`` that core must use to discover
# workloads (ADR-0002) is not a violation.
_WORKLOADS_PATH = re.compile(
    r"(?<![\w.])workloads(?=[./\\])|(?<![\w.])(?:import|from)\s+workloads\b"
)

# Known limits of a static scan (residual risk, covered by review): imports computed at runtime
# from non-literal strings (e.g. "work" + "loads"), and prose in non-Python files that splits a
# term across lines or separates its parts with spaces ("restart service").

CONTRACT_CORE_TO_WORKLOADS = "Tether core must not import workloads (ADR-0002, T-14)"
CONTRACT_WORKLOADS_TO_CORE = "Workloads may import only tether.sdk from Tether (ADR-0002, T-14)"


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def _is_workloads_module(name: str) -> bool:
    return name == "workloads" or name.startswith("workloads.")


def _scan_python_ast(path: Path, text: str) -> list[Violation]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, "file could not be parsed for the boundary scan")]
    found: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                Violation(path, node.lineno, f"imports workload package '{alias.name}'")
                for alias in node.names
                if _is_workloads_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _is_workloads_module(node.module):
                found.append(
                    Violation(path, node.lineno, f"imports from workload package '{node.module}'")
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # String constants are checked whole, so terms split across implicitly
            # concatenated literals on separate lines are still found.
            if _is_workloads_module(node.value.strip()):
                found.append(Violation(path, node.lineno, f"names workload module {node.value!r}"))
            found.extend(
                Violation(path, node.lineno, f"string contains reference-workload term '{term}'")
                for term, pattern in _TERM_PATTERNS.items()
                if pattern.search(node.value)
            )
    return found


def _scan_text(path: Path, text: str) -> list[Violation]:
    found: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        found.extend(
            Violation(path, lineno, f"contains reference-workload term '{term}'")
            for term, pattern in _TERM_PATTERNS.items()
            if pattern.search(line)
        )
        if _WORKLOADS_PATH.search(line):
            found.append(Violation(path, lineno, "references the workloads package by path"))
    return found


def scan_core(root: Path) -> tuple[list[Violation], int]:
    """Scan every file under ``root``; return (violations, number of Python files scanned)."""
    violations: list[Violation] = []
    python_files = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if "__pycache__" in path.parts:
            continue
        relative_name = path.relative_to(root).as_posix()
        violations.extend(
            Violation(path, 0, f"file or directory name contains reference-workload term '{term}'")
            for term, pattern in _TERM_PATTERNS.items()
            if pattern.search(relative_name)
        )
        if _WORKLOADS_PATH.search(relative_name):
            violations.append(Violation(path, 0, "file or directory name references workloads"))
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            python_files += 1
            violations.extend(_scan_python_ast(path, text))
        violations.extend(_scan_text(path, text))
    return violations, python_files


def _importlinter_config() -> dict[str, object]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    importlinter = config.get("tool", {}).get("importlinter")
    assert importlinter, "pyproject.toml has no [tool.importlinter] configuration"
    return dict(importlinter)


def _contract(name: str) -> dict[str, object]:
    contracts = _importlinter_config().get("contracts", [])
    assert isinstance(contracts, list)
    matches = [c for c in contracts if isinstance(c, dict) and c.get("name") == name]
    assert len(matches) == 1, f"expected exactly one import-linter contract named {name!r}"
    return matches[0]


def _core_top_level_modules(root: Path) -> set[str]:
    """Every importable module directly under ``tether`` (subpackages and plain modules)."""
    modules = {
        f"tether.{p.name}" for p in root.iterdir() if p.is_dir() and (p / "__init__.py").exists()
    }
    modules |= {f"tether.{p.stem}" for p in root.glob("*.py") if p.name != "__init__.py"}
    return modules


# --------------------------------------------------------------------------------------------
# Checks against the real repository
# --------------------------------------------------------------------------------------------


def test_core_package_exists() -> None:
    assert (CORE_ROOT / "__init__.py").is_file(), f"core package missing at {CORE_ROOT}"


def test_core_contains_no_workload_references() -> None:
    violations, python_files = scan_core(CORE_ROOT)
    assert python_files > 0, "boundary scan found no Python files; it would pass vacuously"
    assert not violations, "workload references found in core:\n" + "\n".join(map(str, violations))


def test_importlinter_root_packages_cover_core_and_workloads() -> None:
    roots = _importlinter_config().get("root_packages")
    assert isinstance(roots, list)
    assert {"tether", "workloads"} <= set(roots)


def test_core_must_not_import_workloads_contract_is_configured() -> None:
    contract = _contract(CONTRACT_CORE_TO_WORKLOADS)
    assert contract.get("type") == "forbidden"
    assert contract.get("source_modules") == ["tether"]
    assert contract.get("forbidden_modules") == ["workloads"]
    assert not contract.get("ignore_imports"), (
        "the core-to-workloads contract must have no exceptions"
    )


def test_no_contract_allows_indirect_imports() -> None:
    contracts = _importlinter_config().get("contracts", [])
    assert isinstance(contracts, list)
    for contract in contracts:
        assert isinstance(contract, dict)
        assert contract.get("allow_indirect_imports") in (None, False, "False", "false"), (
            f"contract {contract.get('name')!r} allows indirect imports, which opens a back door "
            "through intermediate modules"
        )


def test_workloads_contract_forbids_every_core_module_except_sdk() -> None:
    contract = _contract(CONTRACT_WORKLOADS_TO_CORE)
    assert contract.get("type") == "forbidden"
    assert contract.get("source_modules") == ["workloads"]
    assert not contract.get("ignore_imports"), "the workloads contract must have no exceptions"
    expected = _core_top_level_modules(CORE_ROOT) - {"tether.sdk"}
    configured = contract.get("forbidden_modules")
    assert isinstance(configured, list)
    assert set(configured) == expected, (
        "forbidden_modules must list every top-level core module except tether.sdk; "
        f"missing={sorted(expected - set(configured))} extra={sorted(set(configured) - expected)}"
    )


# --------------------------------------------------------------------------------------------
# Negative self-tests: the static scanner
# --------------------------------------------------------------------------------------------


def _write_core(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "tether"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "probe.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return root


@pytest.mark.parametrize("term", DOMAIN_TERMS)
def test_scanner_flags_every_domain_term(tmp_path: Path, term: str) -> None:
    root = _write_core(tmp_path, f"# handles the {term.upper()} case\nVALUE = 1\n")
    violations, _ = scan_core(root)
    assert any(term in v.reason for v in violations), f"scanner missed domain term {term!r}"


@pytest.mark.parametrize(
    "source",
    [
        "import workloads\n",
        "import workloads.incident_remediation as wl\n",
        "from workloads.incident_remediation import tools\n",
        "from workloads import incident_remediation\n",
        "import importlib\nmodule = importlib.import_module('workloads.example')\n",
        "module = __import__('workloads')\n",
        "PATH = 'workloads/example/tools.py'\n",
    ],
)
def test_scanner_flags_workload_package_references(tmp_path: Path, source: str) -> None:
    violations, _ = scan_core(_write_core(tmp_path, source))
    assert violations, f"scanner missed a workload reference in:\n{source}"


@pytest.mark.parametrize(
    "source",
    [
        # camelCase / PascalCase / acronym-prefixed identifiers
        "class ServiceIncident:\n    pass\n",
        "def getRunbook() -> None:\n    pass\n",
        "class HTTPIncident:\n    pass\n",
        "RestartService = 1\n",
        "isOnCall = True\n",
        "DEMO_TARGET_URL = ''\n",
        # separator variants
        "VALUE = 'on-call'\n",
        "VALUE = 'on_call'\n",
        "VALUE = 'ops-demo'\n",
        "VALUE = 'update-service-config'\n",
        # a term split across implicitly concatenated literals on separate lines
        "VALUE = (\n    'restart_'\n    'service'\n)\n",
        # a workload import hidden inside a string
        "exec('import workloads')\n",
    ],
)
def test_scanner_flags_evasive_spellings(tmp_path: Path, source: str) -> None:
    violations, _ = scan_core(_write_core(tmp_path, source))
    assert violations, f"scanner missed an evasive reference in:\n{source}"


@pytest.mark.parametrize(
    "relative_path",
    ["core/incident_store.py", "core/runbooks/__init__.py", "core/OnCallRota.py"],
)
def test_scanner_flags_domain_terms_in_file_and_directory_names(
    tmp_path: Path, relative_path: str
) -> None:
    root = _write_core(tmp_path, "VALUE = 1\n")
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    violations, _ = scan_core(root)
    assert any(v.path == target for v in violations), f"scanner missed the name {relative_path!r}"


def test_scanner_flags_workload_imports_in_non_python_files(tmp_path: Path) -> None:
    root = _write_core(tmp_path, "VALUE = 1\n")
    (root / "notes.txt").write_text("import workloads\n", encoding="utf-8")
    violations, _ = scan_core(root)
    assert any(v.path.name == "notes.txt" for v in violations)


def test_scanner_flags_terms_in_non_python_files(tmp_path: Path) -> None:
    root = _write_core(tmp_path, "VALUE = 1\n")
    (root / "defaults.yaml").write_text("tool: restart_service\n", encoding="utf-8")
    violations, _ = scan_core(root)
    assert any(v.path.name == "defaults.yaml" for v in violations)


@pytest.mark.parametrize(
    "source",
    [
        # Core discovers workloads through this entry-point group name (ADR-0002).
        "ENTRY_POINT_GROUP = 'tether.workloads'\n",
        # Generic, domain-free vocabulary is allowed.
        "# A workload registers tools and policies through the public SDK.\nVALUE = 1\n",
        "# Two events may be coincident.\nVALUE = 1\n",
        # Words that merely contain "oncall" are not the on-call term.
        "functionCall = 1\nfunction_call = 2\n",
        # Prose with spaces is not treated as the joined term.
        "# Decided on call hash equality.\nVALUE = 1\n",
    ],
)
def test_scanner_allows_generic_vocabulary(tmp_path: Path, source: str) -> None:
    violations, _ = scan_core(_write_core(tmp_path, source))
    assert not violations, "\n".join(map(str, violations))


# --------------------------------------------------------------------------------------------
# Negative self-tests: the import-linter contracts
# --------------------------------------------------------------------------------------------


def _lint_imports_executable() -> str:
    executable = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    assert executable, "lint-imports is not installed in the active environment"
    return executable


def _render_ini(config: dict[str, object]) -> str:
    """Render the pyproject import-linter configuration as an equivalent INI file."""

    def block(values: object) -> str:
        assert isinstance(values, list)
        return "".join(f"\n    {value}" for value in values)

    lines = ["[importlinter]", f"root_packages ={block(config['root_packages'])}"]
    contracts = config["contracts"]
    assert isinstance(contracts, list)
    for index, contract in enumerate(contracts, start=1):
        assert isinstance(contract, dict)
        lines.append(f"\n[importlinter:contract:{index}]")
        for key, value in contract.items():
            lines.append(
                f"{key} ={block(value)}" if isinstance(value, list) else f"{key} = {value}"
            )
    return "\n".join(lines) + "\n"


def _mirror_project(tmp_path: Path) -> Path:
    """Copy the real package layout (empty modules) plus the real contracts into tmp_path."""
    project = tmp_path / "project"
    core = project / "tether"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    for module in sorted(_core_top_level_modules(CORE_ROOT) | {"tether.sdk"}):
        package = core / module.removeprefix("tether.")
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
    workload = project / "workloads" / "example_workload"
    workload.mkdir(parents=True)
    (project / "workloads" / "__init__.py").write_text("", encoding="utf-8")
    (workload / "__init__.py").write_text("", encoding="utf-8")
    (project / ".importlinter").write_text(_render_ini(_importlinter_config()), encoding="utf-8")
    return project


def _run_lint_imports(project: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # import-linter prints non-ASCII; force UTF-8 so Windows (cp1252) can encode/decode it.
    env["PYTHONUTF8"] = "1"
    return subprocess.run(  # noqa: S603 - fixed executable from the active venv, no shell
        [_lint_imports_executable(), "--config", ".importlinter", "--no-cache"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_import_contracts_pass_on_a_clean_mirror(tmp_path: Path) -> None:
    project = _mirror_project(tmp_path)
    (project / "workloads" / "example_workload" / "uses_sdk.py").write_text(
        "import tether.sdk\n", encoding="utf-8"
    )
    result = _run_lint_imports(project)
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_contracts_fail_when_core_imports_a_workload(tmp_path: Path) -> None:
    project = _mirror_project(tmp_path)
    (project / "tether" / "core" / "leak.py").write_text(
        "import workloads.example_workload\n", encoding="utf-8"
    )
    result = _run_lint_imports(project)
    assert result.returncode != 0, "import-linter accepted core importing a workload"
    assert CONTRACT_CORE_TO_WORKLOADS in result.stdout


def test_import_contracts_fail_when_a_workload_imports_core_internals(tmp_path: Path) -> None:
    project = _mirror_project(tmp_path)
    (project / "workloads" / "example_workload" / "leak.py").write_text(
        "import tether.core\n", encoding="utf-8"
    )
    result = _run_lint_imports(project)
    assert result.returncode != 0, "import-linter accepted a workload importing tether.core"
    assert CONTRACT_WORKLOADS_TO_CORE in result.stdout
