"""Configuration chooses the provider (ADR-0016 rule 6, TDR-003, plan W3.3).

Switching provider must be a configuration change only: no runtime, governance, tool or workload
code is touched. These tests pin that the seam is the factory and the settings, and that both
providers fail closed when their configuration is incomplete or forbidden.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from tether.core.errors import SecretNotFoundError
from tether.providers.factory import ProviderConfigurationError, build_provider
from tether.providers.openai_provider import OpenAIProvider
from tether.secrets.env_provider import EnvSecretsProvider
from tether.settings import Settings
from tether.testing.scripted import ScenarioError, ScriptedProvider

DSN = "postgresql://tether_app:s3cret-value@db:5432/tether"
KEY_ENV = "TETHER_OPENAI_API_KEY"
# Not a credential: a syntactically plausible value for configuration tests.
FAKE_KEY = "sk-test-not-a-real-key-000000000000"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("TETHER_DATABASE_URL", DSN)
    for name in ("TETHER_MODEL_PROVIDER", "TETHER_OPENAI_MODEL", KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings()


def _secrets(**env: str) -> EnvSecretsProvider:
    return EnvSecretsProvider(env)


def test_the_default_provider_is_the_scripted_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The baseline behaviour is unchanged for anyone who sets nothing."""
    settings = _settings(monkeypatch, TETHER_ENV="test")
    assert settings.provider == "scripted"
    assert isinstance(build_provider(settings, secrets=_secrets()), ScriptedProvider)


def test_configuration_selects_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        TETHER_ENV="prod",
        TETHER_MODEL_PROVIDER="openai",
        TETHER_OPENAI_MODEL="a-model",
    )
    assert settings.provider == "openai"
    provider = build_provider(settings, secrets=_secrets(**{KEY_ENV: FAKE_KEY}))
    assert isinstance(provider, OpenAIProvider)
    assert (provider.name, provider.model) == ("openai", "a-model")


def test_the_provider_can_also_be_set_by_field_name() -> None:
    """Integration and chaos helpers build ``Settings`` from keyword arguments.

    The field carries a ``validation_alias`` so the environment variable can stay
    ``TETHER_MODEL_PROVIDER``. Without ``populate_by_name`` an alias makes the field name
    unusable, and ``extra="ignore"`` would drop ``provider=`` silently instead of failing.
    """
    settings = Settings(
        database_url=SecretStr(DSN), env="test", provider="openai", openai_model="a-model"
    )
    assert settings.provider == "openai"


def test_an_unknown_provider_name_is_refused_by_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(Exception, match="provider"):
        _settings(monkeypatch, TETHER_ENV="test", TETHER_MODEL_PROVIDER="not-a-provider")


def test_openai_without_a_model_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No model is defaulted: Tether never spends on a model nobody chose."""
    settings = _settings(monkeypatch, TETHER_ENV="prod", TETHER_MODEL_PROVIDER="openai")
    with pytest.raises(ProviderConfigurationError):
        build_provider(settings, secrets=_secrets(**{KEY_ENV: FAKE_KEY}))


def test_openai_without_a_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        TETHER_ENV="prod",
        TETHER_MODEL_PROVIDER="openai",
        TETHER_OPENAI_MODEL="a-model",
    )
    with pytest.raises(SecretNotFoundError):
        build_provider(settings, secrets=_secrets())


def test_the_key_comes_only_from_the_secrets_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key is not a settings field, so it cannot reach a settings dump or repr."""
    settings = _settings(
        monkeypatch,
        TETHER_ENV="prod",
        TETHER_MODEL_PROVIDER="openai",
        TETHER_OPENAI_MODEL="a-model",
        **{KEY_ENV: FAKE_KEY},
    )
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings.model_dump())
    assert not hasattr(settings, "openai_api_key")


def test_the_scripted_provider_is_still_refused_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, TETHER_ENV="prod")
    with pytest.raises(ScenarioError):
        build_provider(settings, secrets=_secrets())


def test_the_openai_base_url_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-hosted or proxied endpoint is configuration, not code."""
    settings = _settings(
        monkeypatch,
        TETHER_ENV="prod",
        TETHER_MODEL_PROVIDER="openai",
        TETHER_OPENAI_MODEL="a-model",
        TETHER_OPENAI_BASE_URL="https://gateway.example.test/v1",
    )
    provider = build_provider(settings, secrets=_secrets(**{KEY_ENV: FAKE_KEY}))
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://gateway.example.test/v1"


def test_scenarios_dir_is_only_needed_by_the_scripted_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(
        monkeypatch, TETHER_ENV="test", TETHER_SCENARIOS_DIR=str(tmp_path / "scenarios")
    )
    assert settings.scenarios_dir == tmp_path / "scenarios"
    assert isinstance(build_provider(settings, secrets=_secrets()), ScriptedProvider)
