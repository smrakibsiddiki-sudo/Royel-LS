"""Redis-specific error hierarchy."""

from royells_v20_core.errors import (
    CheckpointError,
    ConfigurationError,
    QueueError,
)


class RedisError(Exception):
    """Base Redis infrastructure error."""


class RedisConfigurationError(ConfigurationError, RedisError):
    """Invalid Redis or Upstash configuration."""


class RedisConnectionFailure(RedisError):
    """Redis connection or reconnect failure."""


class RedisTimeout(RedisError):
    """Redis command, pool, or socket timeout."""


class RedisQueueError(QueueError, RedisError):
    """Redis queue command or invariant failure."""


class RedisRuntimeError(CheckpointError, RedisError):
    """Redis runtime state or checksum failure."""


class RedisLockError(RedisError):
    """Distributed lock acquisition or ownership failure."""
