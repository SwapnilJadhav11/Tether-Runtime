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

    worker_poll_interval_seconds: float = 1.0
    # How long a leased job stays invisible to other workers before it can be reclaimed.
    worker_lease_seconds: float = Field(default=60.0, gt=0)
    worker_heartbeat_file: Path = _default_heartbeat_file()
    worker_heartbeat_max_age_seconds: float = 30.0


class MigrationSettings(BaseSettings):
    """Settings for the one-shot ``migrate`` process, which runs as the migrator role."""

    model_config = SettingsConfigDict(env_prefix="TETHER_", extra="ignore")

    migrator_database_url: SecretStr
