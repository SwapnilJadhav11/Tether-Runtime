"""T-32 (W7.1, D-12): ``TETHER_ENV`` is explicit; production fails closed.

``Settings.env`` has no default. A process that connects to the database (``tether-api``,
``tether-worker``, the worker healthcheck and every database-connecting CLI command) refuses to
start without a valid value, with a message that names the variable and never prints a credential.
The dev identity shortcut and the scripted provider are reachable only with an explicit ``dev`` or
``test``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from tether.api import dev_identity
from tether.api.app import create_app
from tether.api.app import run as run_api
from tether.cli.main import app as cli
from tether.providers.factory import ProviderConfigurationError, build_provider
from tether.settings import MigrationSettings, Settings, load_settings
from tether.testing.scripted import ScenarioError, ScriptedProvider
from tether.worker.main import healthcheck
from tether.worker.main import main as run_worker_process

LEAK_MARKER = "p4ss-w0rd-must-not-leak"
DSN = f"postgresql://tether_app:{LEAK_MARKER}@127.0.0.1:1/tether"
MIGRATOR_DSN = f"postgresql://tether_migrator:{LEAK_MARKER}@127.0.0.1:1/tether"

runner = CliRunner()


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A process environment with a database URL and no ``TETHER_ENV`` at all."""
    monkeypatch.delenv("TETHER_ENV", raising=False)
    monkeypatch.setenv("TETHER_DATABASE_URL", DSN)
    monkeypatch.setenv("TETHER_MIGRATOR_DATABASE_URL", MIGRATOR_DSN)
    return monkeypatch


# --- Settings ---------------------------------------------------------------------------------


def test_t32_settings_have_no_default_environment(no_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert [error["loc"] for error in caught.value.errors()] == [("env",)]


def test_t32_settings_built_in_code_must_state_the_environment(
    no_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=SecretStr(DSN))  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["", "production", "Prod", "staging", "dev ", "1"])
def test_t32_an_invalid_environment_is_refused(no_env: pytest.MonkeyPatch, value: str) -> None:
    no_env.setenv("TETHER_ENV", value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["dev", "test", "prod"])
def test_t32_each_valid_environment_is_accepted(no_env: pytest.MonkeyPatch, value: str) -> None:
    no_env.setenv("TETHER_ENV", value)
    assert Settings().env == value


def test_t32_migration_settings_also_require_the_environment(no_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as caught:
        MigrationSettings()
    assert [error["loc"] for error in caught.value.errors()] == [("env",)]


def test_t32_load_settings_exits_nonzero_naming_the_variable_and_no_credential(
    no_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        load_settings()
    assert caught.value.code not in (0, None)
    captured = capsys.readouterr()
    assert "TETHER_ENV" in captured.err
    assert "dev" in captured.err
    assert "prod" in captured.err
    assert LEAK_MARKER not in captured.out + captured.err


def test_t32_load_settings_names_an_invalid_value_without_echoing_it(
    no_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    no_env.setenv("TETHER_ENV", "definitely-not-an-env")
    with pytest.raises(SystemExit):
        load_settings()
    captured = capsys.readouterr()
    assert "TETHER_ENV" in captured.err
    assert LEAK_MARKER not in captured.err


def test_t32_load_settings_returns_settings_when_valid(no_env: pytest.MonkeyPatch) -> None:
    no_env.setenv("TETHER_ENV", "prod")
    assert load_settings().env == "prod"


# --- Process entrypoints ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrypoint", [run_api, run_worker_process, healthcheck], ids=["api", "worker", "healthcheck"]
)
def test_t32_long_running_entrypoints_refuse_to_start_without_the_environment(
    no_env: pytest.MonkeyPatch, entrypoint: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        entrypoint()
    assert caught.value.code not in (0, None)
    captured = capsys.readouterr()
    assert "TETHER_ENV" in captured.err
    assert LEAK_MARKER not in captured.out + captured.err


def test_t32_create_app_without_settings_refuses_a_missing_environment(
    no_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit):
        create_app()


def test_t32_the_api_process_exits_nonzero_without_the_environment() -> None:
    """The real entrypoint, in a real process, with the variable absent."""
    env = {key: value for key, value in os.environ.items() if key != "TETHER_ENV"}
    env["TETHER_DATABASE_URL"] = DSN
    result = subprocess.run(
        [sys.executable, "-c", "from tether.api.app import run; run()"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "TETHER_ENV" in result.stderr
    assert LEAK_MARKER not in result.stdout + result.stderr


# --- CLI --------------------------------------------------------------------------------------


def test_t32_cli_help_works_without_any_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("TETHER_ENV", "TETHER_DATABASE_URL", "TETHER_MIGRATOR_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for args in (["--help"], ["bootstrap", "--help"], ["audit", "--help"], ["apikey", "--help"]):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, (args, result.output)


def test_t32_migrate_refuses_without_the_environment(no_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli, ["migrate"])
    assert result.exit_code != 0
    assert "TETHER_ENV" in result.output
    assert LEAK_MARKER not in result.output


def test_t32_bootstrap_apply_refuses_without_the_environment(
    no_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text("workspaces: []\n", encoding="utf-8")
    result = runner.invoke(cli, ["bootstrap", "apply", str(bootstrap)])
    assert result.exit_code != 0
    assert "TETHER_ENV" in result.output
    assert LEAK_MARKER not in result.output


def test_t32_audit_verify_refuses_without_the_environment(no_env: pytest.MonkeyPatch) -> None:
    result = runner.invoke(
        cli,
        ["audit", "verify", "--workspace", "w", "--run", "00000000-0000-0000-0000-000000000001"],
    )
    assert result.exit_code != 0
    assert "TETHER_ENV" in result.output
    assert LEAK_MARKER not in result.output


# --- The dev identity shortcut ----------------------------------------------------------------


def _request_with(env: str) -> Any:
    settings = Settings(database_url=SecretStr(DSN), env=env)  # type: ignore[arg-type]
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


@pytest.mark.parametrize("env", ["dev", "test"])
def test_t32_the_dev_identity_shortcut_is_available_only_in_dev_and_test(env: str) -> None:
    assert dev_identity.allowed(_request_with(env)) is True


def test_t32_the_dev_identity_shortcut_is_refused_in_prod() -> None:
    assert dev_identity.allowed(_request_with("prod")) is False


# --- The scripted provider and scenario files -------------------------------------------------


@pytest.mark.parametrize("env", ["prod", "", "production", "staging", "Dev"])
def test_t32_the_scripted_provider_is_refused_outside_dev_and_test(
    tmp_path: Path, env: str
) -> None:
    (tmp_path / "x.yaml").write_text("name: x\nturns: []\n", encoding="utf-8")
    with pytest.raises(ScenarioError):
        ScriptedProvider(tmp_path, env=env)


@pytest.mark.parametrize("env", ["dev", "test"])
def test_t32_the_scripted_provider_loads_with_an_explicit_dev_or_test(
    tmp_path: Path, env: str
) -> None:
    assert ScriptedProvider(tmp_path, env=env).name == "scripted"


def test_t32_the_factory_refuses_the_scripted_provider_in_prod(tmp_path: Path) -> None:
    settings = Settings(
        database_url=SecretStr(DSN), env="prod", provider="scripted", scenarios_dir=tmp_path
    )
    with pytest.raises(ScenarioError):
        build_provider(settings)


def test_t32_the_factory_refuses_the_scripted_provider_in_prod_before_reading_scenarios(
    tmp_path: Path,
) -> None:
    """A production process never touches the scenario directory, not even to look."""
    missing = tmp_path / "does-not-exist"
    settings = Settings(
        database_url=SecretStr(DSN), env="prod", provider="scripted", scenarios_dir=missing
    )
    with pytest.raises(ScenarioError, match="dev or test only"):
        build_provider(settings)
    assert not missing.exists()


def test_t32_a_build_without_the_scripted_module_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The production image removes the module; the factory then reports it, it does not crash."""
    monkeypatch.setitem(sys.modules, "tether.testing.scripted", None)
    settings = Settings(
        database_url=SecretStr(DSN), env="dev", provider="scripted", scenarios_dir=tmp_path
    )
    with pytest.raises(ProviderConfigurationError, match="not part of this build"):
        build_provider(settings)
