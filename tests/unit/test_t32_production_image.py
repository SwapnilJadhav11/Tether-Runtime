"""T-32 (W7.2, D-12): the production image variant carries no demo or dev components.

Static proof, from the Dockerfile and the packaging configuration, with no Docker needed: the
``prod`` target is built from stages that copy none of the excluded paths and that delete the demo
workload, every scenario directory and the scripted provider before the wheel is built. The
companion test ``tests/integration/test_production_image_built.py`` inspects a really built image.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
PYPROJECT = ROOT / "pyproject.toml"

# Paths that exist only for development, demonstration or testing, relative to the repository.
EXCLUDED_FROM_PROD = ("config", "scenarios", "tools", "services", "deploy", "tests", "docs")
DEMO_WORKLOAD = "workloads/incident_remediation"
SCRIPTED_MODULE = "src/tether/testing/scripted.py"


@dataclass
class Stage:
    name: str
    base: str
    copies: list[tuple[str | None, list[str], str]] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        lines.append((pending + stripped).strip())
        pending = ""
    return lines


def parse_dockerfile(path: Path = DOCKERFILE) -> dict[str, Stage]:
    stages: dict[str, Stage] = {}
    current: Stage | None = None
    for line in _logical_lines(path.read_text(encoding="utf-8")):
        keyword, _, rest = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            match = re.match(r"(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?$", rest.strip(), re.IGNORECASE)
            assert match, f"cannot parse FROM {rest!r}"
            current = Stage(name=match.group(2) or match.group(1), base=match.group(1))
            stages[current.name] = current
        elif current is not None and keyword == "COPY":
            from_stage = None
            args = rest.split()
            while args and args[0].startswith("--"):
                flag = args.pop(0)
                if flag.startswith("--from="):
                    from_stage = flag.removeprefix("--from=")
            current.copies.append((from_stage, args[:-1], args[-1]))
        elif current is not None and keyword == "RUN":
            current.runs.append(rest)
    return stages


def lineage(stages: dict[str, Stage], name: str) -> list[Stage]:
    """The stage, its base stages and every stage it copies files from."""
    seen: dict[str, Stage] = {}
    todo = [name]
    while todo:
        current = todo.pop()
        if current in seen or current not in stages:
            continue
        seen[current] = stages[current]
        todo.append(stages[current].base)
        todo.extend(source for source, _, _ in stages[current].copies if source)
    return list(seen.values())


@pytest.fixture(scope="module")
def stages() -> dict[str, Stage]:
    return parse_dockerfile()


def _copied_sources(stage_list: list[Stage]) -> list[str]:
    return [
        source
        for stage in stage_list
        for from_stage, sources, _ in stage.copies
        if from_stage is None
        for source in sources
    ]


def _normalise(path: str) -> str:
    return path.removeprefix("./").rstrip("/")


# --- The Dockerfile has two targets, and the default is the dev image -------------------------


def test_t32_the_dockerfile_defines_a_prod_and_a_dev_target(stages: dict[str, Stage]) -> None:
    assert "prod" in stages
    assert "dev" in stages


def test_t32_the_dev_stage_is_last_so_a_plain_build_stays_the_dev_image(
    stages: dict[str, Stage],
) -> None:
    assert list(stages)[-1] == "dev"


def test_t32_the_two_variants_do_not_share_a_build_stage(stages: dict[str, Stage]) -> None:
    prod = {stage.name for stage in lineage(stages, "prod")}
    dev = {stage.name for stage in lineage(stages, "dev")}
    shared = prod & dev
    # They may share the dependency layer and the runtime base, but never the stage that holds
    # the application source: that is where the variants differ.
    for name in shared:
        assert all(
            "src" not in map(_normalise, sources) and "workloads" not in map(_normalise, sources)
            for from_stage, sources, _ in stages[name].copies
            if from_stage is None
        ), f"stage {name!r} is shared by both variants and copies application source"


# --- The production lineage copies none of the excluded paths ---------------------------------


def test_t32_the_prod_lineage_copies_no_excluded_path(stages: dict[str, Stage]) -> None:
    sources = [_normalise(source) for source in _copied_sources(lineage(stages, "prod"))]
    for source in sources:
        for excluded in EXCLUDED_FROM_PROD:
            assert source != excluded
            assert not source.startswith(f"{excluded}/"), source
            assert f"/{excluded}" not in f"/{source}", source


def test_t32_the_prod_lineage_copies_no_environment_or_key_file(stages: dict[str, Stage]) -> None:
    for source in _copied_sources(lineage(stages, "prod")):
        name = Path(source).name
        assert not name.startswith(".env"), source
        assert ".keys" not in source
        assert not source.endswith((".pem", ".key")), source


def test_t32_the_prod_lineage_does_not_copy_the_build_context_wholesale(
    stages: dict[str, Stage],
) -> None:
    for source in _copied_sources(lineage(stages, "prod")):
        assert _normalise(source) not in {".", "*", "./", ""}, source


def test_t32_the_prod_target_sets_no_environment_and_runs_no_dev_command(
    stages: dict[str, Stage],
) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    prod_text = "\n".join(" ".join(stage.runs) for stage in lineage(stages, "prod"))
    assert "TETHER_ENV" not in text, "the image must never choose its own environment"
    assert "--group dev" not in prod_text
    assert "--no-dev" in prod_text


# --- The prod build deletes what the source tree would otherwise bring along ------------------


def _prod_build_runs(stages: dict[str, Stage]) -> str:
    return "\n".join(
        run for stage in lineage(stages, "prod") for run in stage.runs if "uv sync" in run
    )


def test_t32_the_prod_build_removes_the_demo_workload_and_every_scenario_directory(
    stages: dict[str, Stage],
) -> None:
    runs = _prod_build_runs(stages)
    assert DEMO_WORKLOAD in runs
    deleted = re.findall(r"workloads/[\w./-]+", runs)
    for scenarios in sorted((ROOT / "workloads").glob("*/scenarios")):
        relative = scenarios.relative_to(ROOT).as_posix()
        # Either the directory itself, or the demo workload directory that contains it.
        assert any(relative == path or relative.startswith(f"{path}/") for path in deleted), (
            f"the prod build must delete {relative}"
        )


def test_t32_the_prod_build_removes_the_scripted_provider_module(stages: dict[str, Stage]) -> None:
    assert SCRIPTED_MODULE in _prod_build_runs(stages)
    assert (ROOT / SCRIPTED_MODULE).is_file(), "the path the prod build deletes must still exist"


def test_t32_every_path_the_prod_build_deletes_exists_in_the_repository(
    stages: dict[str, Stage],
) -> None:
    """A rename must not silently turn a deletion into a no-op (``rm -rf`` never complains)."""
    deleted: list[str] = []
    for stage in lineage(stages, "prod"):
        for run in stage.runs:
            for match in re.finditer(r"rm\s+(?:-\S+\s+)*((?:[\w./-]+\s*)+?)(?:&&|;|$)", run):
                deleted.extend(match.group(1).split())
    assert deleted, "the prod build deletes nothing"
    for path in deleted:
        assert (ROOT / path).exists(), f"{path} is deleted by the prod build but does not exist"


def test_t32_the_prod_build_strips_the_demo_workload_entry_point(stages: dict[str, Stage]) -> None:
    runs = _prod_build_runs(stages)
    pattern = re.search(r"sed -i '/(\^[^/]+)/d' pyproject\.toml", runs)
    assert pattern, "the prod build must remove the demo workload's entry point from pyproject.toml"
    matching = [
        line
        for line in PYPROJECT.read_text(encoding="utf-8").splitlines()
        if re.search(pattern.group(1), line)
    ]
    assert matching == ['incident_remediation = "workloads.incident_remediation:WORKLOAD"']


# --- What is left after the deletions is free of demo and dev content -------------------------


def _remaining_workload_files(stages: dict[str, Stage]) -> list[Path]:
    removed = [
        path
        for stage in lineage(stages, "prod")
        for run in stage.runs
        for path in re.findall(r"workloads/[\w./-]+", run)
    ]
    kept: list[Path] = []
    for path in (ROOT / "workloads").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if any(relative == r or relative.startswith(f"{r}/") for r in removed):
            continue
        kept.append(path)
    return kept


def test_t32_no_scenario_file_remains_in_the_prod_workloads(stages: dict[str, Stage]) -> None:
    kept = _remaining_workload_files(stages)
    assert kept, "the real workload must remain"
    assert [p for p in kept if "scenarios" in p.parts] == []
    assert [p for p in kept if p.suffix in {".yaml", ".yml"}] == []


def test_t32_the_remaining_workloads_do_not_import_the_demo_workload(
    stages: dict[str, Stage],
) -> None:
    for path in _remaining_workload_files(stages):
        if path.suffix == ".py":
            assert "incident_remediation" not in path.read_text(encoding="utf-8"), path


def test_t32_the_core_imports_the_scripted_provider_only_lazily() -> None:
    """With the module deleted from the prod image, nothing may import it at module level."""
    importers = []
    for path in (ROOT / "src").rglob("*.py"):
        if path.relative_to(ROOT).as_posix() == SCRIPTED_MODULE:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "tether.testing.scripted" in line and line.startswith(("from ", "import ")):
                importers.append(path.relative_to(ROOT).as_posix())
    assert importers == []


def test_t32_the_wheel_packages_are_the_only_things_the_build_ships() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/tether",
        "workloads",
    ]


# --- Environment files ------------------------------------------------------------------------


def test_t32_the_production_environment_example_holds_placeholders_only() -> None:
    path = ROOT / "deploy" / "compose" / ".env.production.example"
    text = path.read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    assert values["TETHER_ENV"] == "prod"
    assert values["TETHER_MODEL_PROVIDER"] == "openai"
    # Settings that are not secrets and are the same for every deployment.
    literal = {"TETHER_ENV", "TETHER_MODEL_PROVIDER", "TETHER_LOG_LEVEL", "TETHER_BOOTSTRAP_FILE"}
    for name, value in values.items():
        if name in literal:
            continue
        # Empty, or built only from <placeholders>: a URL keeps its scheme, user and host shape
        # but every credential and host is a placeholder.
        stripped = re.sub(r"<[^>]+>", "", value)
        assert value == "" or (
            stripped != value and not re.search(r"[A-Za-z0-9]{12,}", stripped)
        ), f"{name} must be a placeholder or empty, not a value"
    assert "TETHER_SCENARIOS_DIR" not in values
    assert not re.search(r"sk-[A-Za-z0-9]{8,}", text)
    assert not re.search(r"tak_\w{8,}", text)


def test_t32_the_development_environment_is_named_explicitly_where_it_was_implied() -> None:
    compose = (ROOT / "deploy" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s+TETHER_ENV: dev\s*$", compose, re.MULTILINE)
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^env:\n\s+TETHER_ENV: test\s*$", workflow, re.MULTILINE)
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'os.environ.setdefault("TETHER_ENV", "test")' in conftest


def test_t32_every_tether_service_in_the_compose_stack_names_its_environment() -> None:
    """Each service that runs the Tether image starts a process that refuses to run without
    ``TETHER_ENV`` (W7.1), including the one-shot ``migrate`` service, which has its own
    environment block."""
    compose = yaml.safe_load(
        (ROOT / "deploy" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    tether_services = {
        name: service
        for name, service in compose["services"].items()
        if service.get("image") == "tether-runtime:dev"
    }
    assert {"migrate", "api", "worker"} <= set(tether_services)
    for name, service in tether_services.items():
        assert service.get("environment", {}).get("TETHER_ENV") == "dev", name


def test_t32_the_compose_stack_builds_the_dev_target_explicitly() -> None:
    compose = (ROOT / "deploy" / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s+target: dev\s*$", compose, re.MULTILINE)
