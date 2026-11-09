"""Process settings for the api and worker processes.

Settings are read from ``TETHER_``-prefixed environment variables. Database URLs carry
credentials, so they are ``SecretStr`` and never rendered in reprs, dumps or logs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_heartbeat_file() -> Path:
    return Path(tempfile.gettempdir()) / "tether-worker.heartbeat"


class Settings(BaseSettings):
    """Settings shared by the ``api`` and ``worker`` processes (ADR-0001)."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

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
    # Scenario files for the scripted model provider (dev/test only, PD-23).
    scenarios_dir: Path = Path("scenarios")
    max_model_turns: int = Field(default=8, gt=0)
    # How long an approval request stays decidable (expiry is checked at decision time).
    approval_ttl_seconds: int = Field(default=3600, gt=0)

    worker_poll_interval_seconds: float = 1.0
    # How long a leased job stays invisible to other workers before it can be reclaimed.
    worker_lease_seconds: float = Field(default=60.0, gt=0)
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


class MigrationSettings(BaseSettings):
    """Settings for the one-shot ``migrate`` process, which runs as the migrator role."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", extra="ignore")

    migrator_database_url: SecretStr
