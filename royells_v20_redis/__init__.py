"""Optional Redis/Upstash infrastructure for Royells v20 Book 19."""

from .adapter import RedisAdapter
from .configuration import RedisConfigurationProvider, RedisSettings
from .keyspace import QueueKeys, RedisKeyspace
from .locks import DistributedLockManager, LockHandle
from .managers import (
    CheckpointManager,
    HeartbeatManager,
    ProcessingStateManager,
    WorkerStateManager,
)
from .metrics import RedisMetricsAdapter
from .pubsub import RedisEventBus
from .queue import RedisQueueAdapter
from .runtime import RedisRuntimeAdapter
from .serialization import JsonSerializer
from .upstash_rest import UpstashRestAdapter

__all__ = [
    "RedisAdapter",
    "RedisConfigurationProvider",
    "RedisSettings",
    "QueueKeys",
    "RedisKeyspace",
    "DistributedLockManager",
    "LockHandle",
    "HeartbeatManager",
    "CheckpointManager",
    "WorkerStateManager",
    "ProcessingStateManager",
    "RedisMetricsAdapter",
    "RedisEventBus",
    "RedisQueueAdapter",
    "RedisRuntimeAdapter",
    "UpstashRestAdapter",
    "JsonSerializer",
]
