from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import SecretStr

from tether.api.app import create_app
from tether.settings import Settings

# Nothing listens on port 1, so the database is unreachable.
UNREACHABLE_DSN = SecretStr("postgresql://tether_app:x@127.0.0.1:1/tether")


def _settings() -> Settings:
    return Settings(database_url=UNREACHABLE_DSN, readiness_timeout_seconds=0.5)


def test_healthz_is_200_without_a_database() -> None:
    with TestClient(create_app(_settings())) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_503_when_the_database_is_unreachable() -> None:
    with TestClient(create_app(_settings())) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
