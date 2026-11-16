"""Provider construction from configuration (ADR-0016 rule 6, TDR-003, plan W3.3).

One function, one ``match``. Choosing a provider is a settings change and nothing else: the run
driver asks for a provider and never names one. This is deliberately not a registry, a plugin
manager or a router — adding a provider means adding a branch here and an adapter module.

The API key is read through the ``SecretsProvider``, never from ``Settings``, so it cannot reach
a settings dump, a log line or a repr (ADR-0017, plan W9.1).
"""

from __future__ import annotations

from tether.providers.openai_provider import OpenAIProvider
from tether.providers.protocol import ModelProvider
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.provider import SecretsProvider
from tether.settings import Settings
from tether.testing.scripted import ScriptedProvider

OPENAI_API_KEY_NAME = "TETHER_OPENAI_API_KEY"


class ProviderConfigurationError(Exception):
    """The configured provider cannot be built. Startup fails closed."""


def build_provider(settings: Settings, *, secrets: SecretsProvider | None = None) -> ModelProvider:
    """The provider named by ``settings.provider`` (``TETHER_MODEL_PROVIDER``)."""
    match settings.provider:
        case "scripted":
            # Refuses to load outside an explicit dev/test environment (ADR-0016 rule 6).
            return ScriptedProvider(settings.scenarios_dir, env=settings.env)
        case "openai":
            if not settings.openai_model:
                raise ProviderConfigurationError(
                    "set TETHER_OPENAI_MODEL: the OpenAI provider has no default model"
                )
            source = secrets if secrets is not None else EnvSecretsProvider()
            return OpenAIProvider(
                api_key=source.get(OPENAI_API_KEY_NAME),
                model=settings.openai_model,
                base_url=settings.openai_base_url,
                timeout_seconds=settings.openai_timeout_seconds,
            )
