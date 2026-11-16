"""T-32 (W7.2, D-12): inspect a really built production image.

Builds ``--target prod`` and looks inside it. Skipped when Docker is unavailable, and when
``TETHER_SKIP_IMAGE_BUILD`` is set (for hosts where a build is not wanted). The static proof from
the Dockerfile is ``tests/unit/test_t32_production_image.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")


def _docker_usable() -> bool:
    if os.environ.get("TETHER_SKIP_IMAGE_BUILD") or DOCKER is None:
        return False
    try:
        probe = subprocess.run(  # noqa: S603 - the resolved docker binary, fixed arguments
            [str(DOCKER), "info", "--format", "{{.OSType}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0 and probe.stdout.strip() == "linux"


pytestmark = pytest.mark.skipif(
    not _docker_usable(), reason="a Linux Docker daemon is required to build the image"
)

INSPECT = """
import importlib.metadata as md
import importlib.util
import os
import sys

problems = []
for path in ("/app/scenarios", "/app/config", "/app/tools", "/app/services"):
    if os.path.exists(path):
        problems.append("present: " + path)
for module in (
    "workloads.incident_remediation",
    "tether.testing.scripted",
):
    try:
        found = importlib.util.find_spec(module)
    except ModuleNotFoundError:
        found = None
    if found is not None:
        problems.append("importable: " + module)
names = {entry.name for entry in md.entry_points(group="tether.workloads")}
if "incident_remediation" in names:
    problems.append("entry point: incident_remediation")
if "change_control" not in names:
    problems.append("missing entry point: change_control")
site = next(p for p in sys.path if p.endswith("site-packages"))
for root, _dirs, files in os.walk(site):
    if os.path.basename(root) == "scenarios":
        problems.append("scenario directory: " + root)
    for name in files:
        if name in ("dev.yaml", "change-control.yaml"):
            problems.append("bootstrap file: " + os.path.join(root, name))
print("\\n".join(problems) or "clean")
"""


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    tag = f"tether-runtime-prod-inspect:{uuid.uuid4().hex[:12]}"
    build = subprocess.run(  # noqa: S603 - the resolved docker binary, fixed arguments
        [str(DOCKER), "build", "--target", "prod", "--tag", tag, str(ROOT)],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert build.returncode == 0, build.stdout[-2000:] + build.stderr[-2000:]
    try:
        yield tag
    finally:
        subprocess.run(  # noqa: S603 - the resolved docker binary, fixed arguments
            [str(DOCKER), "rmi", "--force", tag], capture_output=True, check=False
        )


def _run(
    image: str, *command: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    args = [str(DOCKER), "run", "--rm"]
    for name, value in (env or {}).items():
        args += ["--env", f"{name}={value}"]
    return subprocess.run(  # noqa: S603 - the resolved docker binary, fixed arguments
        [*args, "--entrypoint", command[0], image, *command[1:]],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_t32_the_built_prod_image_contains_no_demo_or_dev_component(image: str) -> None:
    result = _run(image, "python", "-I", "-c", INSPECT)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean", result.stdout


def test_t32_the_built_prod_image_refuses_to_start_without_an_environment(image: str) -> None:
    for entrypoint in ("tether-api", "tether-worker"):
        result = _run(
            image,
            entrypoint,
            env={"TETHER_DATABASE_URL": "postgresql://tether_app:placeholder@127.0.0.1:1/tether"},
        )
        assert result.returncode != 0
        assert "TETHER_ENV" in result.stderr
        assert "placeholder" not in result.stdout + result.stderr


def test_t32_the_built_prod_image_refuses_the_scripted_provider_in_prod(image: str) -> None:
    check = (
        "from pydantic import SecretStr\n"
        "from tether.providers.factory import ProviderConfigurationError, build_provider\n"
        "from tether.settings import Settings\n"
        "s = Settings(database_url=SecretStr('postgresql://x'), env='prod', provider='scripted')\n"
        "try:\n"
        "    build_provider(s)\n"
        "except ProviderConfigurationError as error:\n"
        "    print('refused:', error)\n"
    )
    result = _run(image, "python", "-I", "-c", check)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("refused:")
