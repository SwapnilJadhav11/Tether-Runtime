"""SecretsProvider protocol and SecretValue (ADR-0017, AD-14).

A ``SecretValue`` never renders its value: ``repr``, ``str`` and formatting are masked, and it
cannot be pickled. Only ``reveal()`` returns the value. Registering known secret values with the
log and trace scrubber is part of the observability step (S1.9).
"""

from __future__ import annotations

from typing import NoReturn, Protocol

MASK = "**********"


class SecretValue[T: (str, bytes)]:
    __slots__ = ("_value",)
    _value: T

    def __init__(self, value: T) -> None:
        self._value = value

    def reveal(self) -> T:
        return self._value

    def __repr__(self) -> str:
        return f"SecretValue({MASK!r})"

    def __str__(self) -> str:
        return MASK

    def __format__(self, format_spec: str) -> str:
        return MASK

    def __reduce__(self) -> NoReturn:
        raise TypeError("SecretValue cannot be pickled")

    def __getstate__(self) -> NoReturn:
        raise TypeError("SecretValue does not expose its state")

    def __copy__(self) -> SecretValue[T]:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> SecretValue[T]:
        return self


class SecretsProvider(Protocol):
    def get(self, name: str) -> SecretValue[str]:
        """Return the named secret; raise ``SecretNotFoundError`` if it is not available."""
        ...
