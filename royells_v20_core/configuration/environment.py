"""Environment access is isolated in this module."""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from ..errors import ConfigurationError


class EnvironmentReader:
    """Validated read-only view over environment variables."""

    TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
    FALSE_VALUES = frozenset({"0", "false", "no", "off"})

    def __init__(self, environ: Optional[Mapping[str, str]] = None) -> None:
        self._environ = dict(os.environ if environ is None else environ)

    def raw(self, name: str, default: Any = None) -> Any:
        return self._environ.get(name, default)

    def string(self, name: str, default: str = "") -> str:
        value = self.raw(name, default)
        return str(value if value is not None else default).strip()

    def boolean(self, name: str, default: bool = False) -> bool:
        value = self.raw(name)
        if value is None:
            return bool(default)
        normalized = str(value).strip().lower()
        if normalized in self.TRUE_VALUES:
            return True
        if normalized in self.FALSE_VALUES:
            return False
        raise ConfigurationError(f"{name} must be a boolean value")

    def integer(
        self,
        name: str,
        default: int,
        *,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
    ) -> int:
        raw = self.raw(name, default)
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{name} must be an integer") from exc
        if minimum is not None and value < minimum:
            raise ConfigurationError(f"{name} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ConfigurationError(f"{name} must be <= {maximum}")
        return value

    def floating(
        self,
        name: str,
        default: float,
        *,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> float:
        raw = self.raw(name, default)
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{name} must be numeric") from exc
        if minimum is not None and value < minimum:
            raise ConfigurationError(f"{name} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ConfigurationError(f"{name} must be <= {maximum}")
        return value
