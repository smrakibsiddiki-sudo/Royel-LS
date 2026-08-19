"""Legacy-compatible infrastructure adapters for Book 17."""

from .json_runtime import JsonRuntimeAdapter
from .legacy_bot import (
    LegacyCheckpointAdapter,
    LegacyDatabaseAdapter,
    LegacyDeliveryIntentRepository,
    LegacyLoggerAdapter,
    LegacyMetricsAdapter,
    LegacyQueueAdapter,
    LegacyQueueRegistry,
    LegacyRecoveryAdapter,
    LegacyRuntimeAdapter,
)
from .local_storage import LocalStorageAdapter
from .metrics import InMemoryMetrics
from .pyrogram_telegram import TelegramPyrogramAdapter
from .sqlite_database import SQLiteAdapter
from .sqlite_queue import SQLiteQueueAdapter
from .stdlib_logging import StdlibLoggerAdapter

__all__ = [
    "JsonRuntimeAdapter",
    "LegacyCheckpointAdapter",
    "LegacyDatabaseAdapter",
    "LegacyDeliveryIntentRepository",
    "LegacyLoggerAdapter",
    "LegacyMetricsAdapter",
    "LegacyQueueAdapter",
    "LegacyQueueRegistry",
    "LegacyRecoveryAdapter",
    "LegacyRuntimeAdapter",
    "LocalStorageAdapter",
    "InMemoryMetrics",
    "TelegramPyrogramAdapter",
    "SQLiteAdapter",
    "SQLiteQueueAdapter",
    "StdlibLoggerAdapter",
]
