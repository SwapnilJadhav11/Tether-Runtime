from __future__ import annotations

import pytest
from pydantic import ValidationError

from tether.settings import MigrationSettings, Settings

DSN = "postgresql://tether_app:s3cret-value@db:5432/tether"


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TETHER_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_settings_read_tether_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TETHER_DATABASE_URL", DSN)
    monkeypatch.setenv("TETHER_API_PORT", "9090")
    settings = Settings()
    assert settings.database_url.get_secret_value() == DSN
    assert settings.api_port == 9090


def test_database_url_is_not_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TETHER_DATABASE_URL", DSN)
    settings = Settings()
    assert "s3cret-value" not in repr(settings)
    assert "s3cret-value" not in str(settings.model_dump())


def test_migrator_url_is_required_for_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TETHER_MIGRATOR_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        MigrationSettings()
