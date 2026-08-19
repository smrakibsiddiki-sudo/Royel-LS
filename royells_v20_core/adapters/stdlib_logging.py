"""Standard-library logger adapter."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..interfaces import LoggerInterface


PERFORMANCE_LEVEL = 25
AUDIT_LEVEL = 35
logging.addLevelName(PERFORMANCE_LEVEL, "PERFORMANCE")
logging.addLevelName(AUDIT_LEVEL, "AUDIT")


class StdlibLoggerAdapter(LoggerInterface):
    """Structured logger with secrets excluded by the caller's field policy."""

    def __init__(self, name: str = "royells") -> None:
        self._logger = logging.getLogger(name)

    @staticmethod
    def _render(message: str, fields: dict[str, Any]) -> str:
        if not fields:
            return str(message)
        safe_fields = {
            key: value
            for key, value in fields.items()
            if key.lower() not in {"password", "token", "secret", "session", "api_hash"}
        }
        return f"{message} | {json.dumps(safe_fields, ensure_ascii=True, sort_keys=True, default=str)}"

    def info(self, message: str, **fields: Any) -> None:
        self._logger.info(self._render(message, fields))

    def warn(self, message: str, **fields: Any) -> None:
        self._logger.warning(self._render(message, fields))

    def error(self, message: str, **fields: Any) -> None:
        self._logger.error(self._render(message, fields))

    def performance(self, message: str, **fields: Any) -> None:
        self._logger.log(PERFORMANCE_LEVEL, self._render(message, fields))

    def audit(self, message: str, **fields: Any) -> None:
        self._logger.log(AUDIT_LEVEL, self._render(message, fields))
