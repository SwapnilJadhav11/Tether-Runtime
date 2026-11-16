"""Process settings for the api and worker processes.

Settings are read from ``TETHER_``-prefixed environment variables. Database URLs carry
credentials, so they are ``SecretStr`` and never rendered in reprs, dumps or logs.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENVIRONMENTS = ("dev", "test", "prod")
EXIT_CONFIGURATION = 2


def _default_heartbeat_file() -> Path:
    return Path(tempfile.gettempdir()) / "tether-worker.heartbeat"


class Settings(BaseSettings):
    """Settings shared by the ``api`` and ``worker`` processes (ADR-0001)."""

    # `populate_by_name` keeps every field settable by its own name as well as by its alias:
    # tests build Settings from keyword arguments, and `extra="ignore"` would otherwise drop an
    # aliased field passed by name without a word.
    model_config = SettingsConfigDict(env_prefix="TETHER_", extra="ignore", populate_by_name=True)

    # No default (W7.1, D-12): the environment is always stated, never assumed. Absent or invalid,
    # the process refuses to start rather than quietly running as `dev`.
    env: Literal["dev", "test", "prod"]

    # Connection string for the least-privilege application role (no DDL).
    database_url: SecretStr

    api_host: str = "0.0.0.0"  # noqa: S104 - the api listens on all interfaces in its container
    api_port: int = 8080

    # Upper bound on how long a readiness/database check may wait for a connection.
    readiness_timeout_seconds: float = 2.0

    # Stored on each run at submission as deadline_at = submission time + this (§11.4).
    run_max_duration_seconds: int = Field(default=3600, gt=0)

    # Bootstrap file (workspaces, agents, policy) loaded by the worker; see `tether bootstrap`.
    bootstrap_file: Path | None = None
    # Which model provider the worker builds (ADR-0016 rule 6): configuration only, no code
    # change. The field is not named `model_provider` because pydantic reserves the `model_`
    # namespace; the environment variable is `TETHER_MODEL_PROVIDER`.
    provider: Literal["scripted", "openai"] = Field(
        default="scripted", validation_alias="TETHER_MODEL_PROVIDER"
    )
    # Scenario files for the scripted model provider (dev/test only, PD-23).
    scenarios_dir: Path = Path("scenarios")

    # OpenAI adapter (TDR-004). The API key is deliberately absent: it is read through the
    # SecretsProvider as TETHER_OPENAI_API_KEY, so it can never reach a settings dump or repr.
    # There is no default model: Tether never spends on a model nobody chose.
    openai_model: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_seconds: float = Field(default=60.0, gt=0)

    # Provider attempts per model turn (W3.3, ADR-0016 rule 3): a retryable error is retried until
    # this many attempts were made, with the PD-13 backoff between them; every attempt is recorded.
    provider_max_attempts: int = Field(default=3, gt=0)

    max_model_turns: int = Field(default=8, gt=0)
    # The repair and denial bound (W3.4, PD-15): invalid proposals (unknown tool, invalid or
    # unparseable arguments) and policy denials a run tolerates. One more ends it `failed`.
    max_repairs: int = Field(default=3, gt=0)
    max_denials: int = Field(default=3, gt=0)
    # How long an approval request stays decidable (expiry is checked at decision time).
    approval_ttl_seconds: int = Field(default=3600, gt=0)

    worker_poll_interval_seconds: float = 1.0
    # How long a leased job stays invisible to other workers before it can be reclaimed.
    worker_lease_seconds: float = Field(default=60.0, gt=0)
    # How often a worker extends the lease of the job it is processing (W6.1). Unset: a third of
    # the lease. Must be below the lease.
    worker_lease_heartbeat_seconds: float | None = Field(default=None, gt=0)
    worker_heartbeat_file: Path = _default_heartbeat_file()
    worker_heartbeat_max_age_seconds: float = 30.0

    # Telemetry (S1.9, best-effort per ADR-0015). Without an endpoint, spans and metrics are
    # still produced (trace context is persisted) but not exported.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # OTLP/HTTP base URL of the collector, e.g. http://otel-collector:4318.
    otel_endpoint: str | None = None
    # Upper bound for one export attempt, retries included: a dead collector never blocks.
    otel_export_timeout_seconds: float = Field(default=2.0, gt=0)
    # How often metrics are exported (and the worker samples queue depth).
    otel_metric_interval_seconds: float = Field(default=15.0, gt=0)

    @model_validator(mode="after")
    def _heartbeat_below_lease(self) -> Settings:
        interval = self.worker_lease_heartbeat_seconds
        if interval is not None and interval >= self.worker_lease_seconds:
            raise ValueError("worker_lease_heartbeat_seconds must be below worker_lease_seconds")
        return self

    @property
    def lease_heartbeat_interval(self) -> float:
        return self.worker_lease_heartbeat_seconds or self.worker_lease_seconds / 3


class MigrationSettings(BaseSettings):
    """Settings for the one-shot ``migrate`` process, which runs as the migrator role."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", extra="ignore")

    # Explicit here too (W7.1): the migrate and bootstrap commands connect to the database.
    env: Literal["dev", "test", "prod"]
    migrator_database_url: SecretStr


def configuration_problems(error: ValidationError) -> list[str]:
    """One line per invalid or missing setting. Names only: a value can be a credential."""
    lines: list[str] = []
    for item in error.errors():
        location = str(item["loc"][0]) if item["loc"] else "settings"
        name = location.upper()
        if not name.startswith("TETHER_"):
            name = f"TETHER_{name}"
        if name == "TETHER_ENV":
            lines.append(f"TETHER_ENV must be set explicitly to one of: {', '.join(ENVIRONMENTS)}")
        elif item["type"] == "missing":
            lines.append(f"{name} is required")
        else:
            lines.append(f"{name} is invalid")
    return lines


class _EnvironmentOnly(BaseSettings):
    """Just ``TETHER_ENV``: lets a command check the environment before reading anything else."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", extra="ignore")

    env: Literal["dev", "test", "prod"]


def require_explicit_environment() -> None:
    """Exit non-zero, naming the variable, unless ``TETHER_ENV`` is explicitly valid (W7.1)."""
    try:
        _EnvironmentOnly()
    except ValidationError as error:
        print(  # noqa: T201 - a startup failure for the operator, before logging exists
            "tether: invalid configuration\n"
            + "\n".join(f"  {line}" for line in configuration_problems(error)),
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIGURATION) from None


def load_settings() -> Settings:
    """Read the process settings, or exit non-zero with a clear message (W7.1).

    The message lists the names of the offending variables and nothing else, so it can never
    carry a database URL or another credential into a container log.
    """
    try:
        return Settings()
    except ValidationError as error:
        print(  # noqa: T201 - a startup failure for the operator, before logging exists
            "tether: invalid configuration\n"
            + "\n".join(f"  {line}" for line in configuration_problems(error)),
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIGURATION) from None
