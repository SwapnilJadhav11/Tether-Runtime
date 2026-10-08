"""SecretsProvider backed by environment variables (V1)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import NoReturn

from tether.core.errors import SecretNotFoundError
from tether.secrets.provider import SecretValue

ENV_PREFIX = "TETHER_"


class EnvSecretsProvider:
    """Reads secrets from a snapshot of the environment taken at construction.

    Only ``TETHER_*`` variables are kept, so unrelated secrets in the process environment never
    sit in this object. It has a masked repr and refuses pickling, so it cannot leak through
    logs, job payloads or checkpoints.

    Rotating a secret takes a process restart, which matches key-ID rotation: a new key ID is
    added and activated, and old IDs stay available until no run references them.
    """

    __slots__ = ("_environ",)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        source = os.environ if environ is None else environ
        self._environ = {
            name: value for name, value in source.items() if name.startswith(ENV_PREFIX)
        }

    def __repr__(self) -> str:
        return "EnvSecretsProvider(<redacted>)"

    def __reduce__(self) -> NoReturn:
        raise TypeError("EnvSecretsProvider cannot be pickled")

    def __getstate__(self) -> NoReturn:
        raise TypeError("EnvSecretsProvider does not expose its state")

    def get(self, name: str) -> SecretValue[str]:
        value = self._environ.get(name)
        if not value:
            raise SecretNotFoundError(f"secret {name!r} is not set")
        return SecretValue(value)
