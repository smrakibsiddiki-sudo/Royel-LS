"""Validated, default-disabled Redis/Upstash configuration."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlparse

from royells_v20_core.configuration.environment import EnvironmentReader

from .errors import RedisConfigurationError


@dataclass(frozen=True)
class RedisSettings:
    """Connection, namespace, retry, queue, runtime, and TTL policy."""

    enabled: bool
    redis_url: str
    production_queue_selected: bool = False
    production_runtime_selected: bool = False
    upstash_rest_url: str = ""
    upstash_rest_token: str = ""
    key_prefix: str = "royells"
    queue_prefix: str = "queue"
    namespace_version: str = "v1"
    max_connections: int = 20
    socket_timeout_seconds: float = 5.0
    socket_connect_timeout_seconds: float = 5.0
    pool_wait_timeout_seconds: float = 5.0
    health_check_interval_seconds: int = 30
    retry_attempts: int = 3
    retry_base_seconds: float = 0.25
    retry_cap_seconds: float = 5.0
    max_payload_bytes: int = 1024 * 1024
    queue_max_attempts: int = 8
    queue_visibility_timeout_seconds: int = 300
    queue_recovery_batch: int = 100
    queue_deduplication_ttl_seconds: int = 604800
    lock_ttl_ms: int = 30000
    lock_wait_timeout_ms: int = 5000
    heartbeat_ttl_seconds: int = 120
    processing_ttl_seconds: int = 86400
    checkpoint_ttl_seconds: int = 0
    metrics_ttl_seconds: int = 604800
    pubsub_enabled: bool = False

    def redacted_url(self) -> str:
        if not self.redis_url:
            return "<disabled>"
        if "@" not in self.redis_url:
            return "<configured>"
        prefix, suffix = self.redis_url.rsplit("@", 1)
        scheme = prefix.split(":", 1)[0]
        return f"{scheme}://***:***@{suffix}"


class RedisConfigurationProvider:
    """Build Redis settings without enabling queue or runtime authority."""

    def __init__(self, environ=None) -> None:
        self._reader = EnvironmentReader(environ)

    def _redis_url(self) -> str:
        direct = self._reader.string("REDIS_URL", "")
        if direct:
            return direct
        host = self._reader.string("REDIS_HOST", "")
        password = self._reader.string("REDIS_PASSWORD", "")
        port = self._reader.integer(
            "REDIS_PORT", 6379, minimum=1, maximum=65535
        )
        database = self._reader.integer("REDIS_DB", 0, minimum=0, maximum=15)
        ssl = self._reader.boolean("REDIS_SSL", True)
        if not host and not password:
            return ""
        if not host or not password:
            raise RedisConfigurationError(
                "REDIS_HOST and REDIS_PASSWORD must be configured together"
            )
        scheme = "rediss" if ssl else "redis"
        return (
            f"{scheme}://:{quote(password, safe='')}@{host}:{port}/{database}"
        )

    @staticmethod
    def _validate_redis_url(redis_url: str) -> None:
        if not redis_url:
            return
        parsed = urlparse(redis_url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise RedisConfigurationError(
                "REDIS_URL must be a valid redis:// or rediss:// URL"
            )
        hostname = parsed.hostname.lower()
        if hostname.endswith(".upstash.io") and parsed.scheme != "rediss":
            raise RedisConfigurationError(
                "Upstash Redis TCP connections must use TLS (rediss://)"
            )

    def settings(self) -> RedisSettings:
        upstash_url = self._reader.string(
            "UPSTASH_URL",
            self._reader.string("UPSTASH_REDIS_REST_URL", ""),
        )
        upstash_token = self._reader.string(
            "UPSTASH_TOKEN",
            self._reader.string("UPSTASH_REDIS_REST_TOKEN", ""),
        )
        if bool(upstash_url) != bool(upstash_token):
            raise RedisConfigurationError(
                "UPSTASH_URL and UPSTASH_TOKEN must be configured together"
            )
        settings = RedisSettings(
            enabled=self._reader.boolean("ENABLE_REDIS_ADAPTER", False),
            redis_url=self._redis_url(),
            production_queue_selected=self._reader.boolean(
                "USE_REDIS_QUEUE", False
            ),
            production_runtime_selected=self._reader.boolean(
                "USE_REDIS_STATE", False
            ),
            upstash_rest_url=upstash_url,
            upstash_rest_token=upstash_token,
            key_prefix=self._reader.string("KEY_PREFIX", "royells"),
            queue_prefix=self._reader.string("QUEUE_PREFIX", "queue"),
            namespace_version=self._reader.string(
                "REDIS_NAMESPACE_VERSION", "v1"
            ),
            max_connections=self._reader.integer(
                "REDIS_MAX_CONNECTIONS", 20, minimum=1, maximum=500
            ),
            socket_timeout_seconds=self._reader.floating(
                "REDIS_SOCKET_TIMEOUT", 5.0, minimum=0.1
            ),
            socket_connect_timeout_seconds=self._reader.floating(
                "REDIS_CONNECT_TIMEOUT", 5.0, minimum=0.1
            ),
            pool_wait_timeout_seconds=self._reader.floating(
                "REDIS_POOL_WAIT_TIMEOUT", 5.0, minimum=0.1
            ),
            health_check_interval_seconds=self._reader.integer(
                "REDIS_HEALTH_CHECK_INTERVAL", 30, minimum=1
            ),
            retry_attempts=self._reader.integer(
                "REDIS_RETRY_ATTEMPTS", 3, minimum=1, maximum=10
            ),
            retry_base_seconds=self._reader.floating(
                "REDIS_RETRY_BASE_SECONDS", 0.25, minimum=0.0
            ),
            retry_cap_seconds=self._reader.floating(
                "REDIS_RETRY_CAP_SECONDS", 5.0, minimum=0.1
            ),
            max_payload_bytes=self._reader.integer(
                "REDIS_MAX_PAYLOAD_BYTES",
                1024 * 1024,
                minimum=1024,
                maximum=16 * 1024 * 1024,
            ),
            queue_max_attempts=self._reader.integer(
                "REDIS_QUEUE_MAX_ATTEMPTS", 8, minimum=1, maximum=100
            ),
            queue_visibility_timeout_seconds=self._reader.integer(
                "REDIS_QUEUE_VISIBILITY_TIMEOUT", 300, minimum=1
            ),
            queue_recovery_batch=self._reader.integer(
                "REDIS_QUEUE_RECOVERY_BATCH", 100, minimum=1, maximum=1000
            ),
            queue_deduplication_ttl_seconds=self._reader.integer(
                "REDIS_QUEUE_DEDUPE_TTL_SECONDS",
                604800,
                minimum=60,
            ),
            lock_ttl_ms=self._reader.integer(
                "REDIS_LOCK_TTL_MS", 30000, minimum=1000
            ),
            lock_wait_timeout_ms=self._reader.integer(
                "REDIS_LOCK_WAIT_TIMEOUT_MS", 5000, minimum=0
            ),
            heartbeat_ttl_seconds=self._reader.integer(
                "REDIS_HEARTBEAT_TTL_SECONDS", 120, minimum=10
            ),
            processing_ttl_seconds=self._reader.integer(
                "REDIS_PROCESSING_TTL_SECONDS", 86400, minimum=60
            ),
            checkpoint_ttl_seconds=self._reader.integer(
                "REDIS_CHECKPOINT_TTL_SECONDS", 0, minimum=0
            ),
            metrics_ttl_seconds=self._reader.integer(
                "REDIS_METRICS_TTL_SECONDS", 604800, minimum=60
            ),
            pubsub_enabled=self._reader.boolean(
                "ENABLE_REDIS_PUBSUB", False
            ),
        )
        if settings.production_queue_selected:
            raise RedisConfigurationError(
                "USE_REDIS_QUEUE is not supported: PostgreSQL is the durable "
                "queue authority and Redis is projection-only"
            )
        if settings.production_runtime_selected:
            raise RedisConfigurationError(
                "USE_REDIS_STATE is not supported: PostgreSQL/legacy JSON remain "
                "the durable runtime authority and Redis is projection-only"
            )
        if settings.enabled and not (
            settings.redis_url
            or (settings.upstash_rest_url and settings.upstash_rest_token)
        ):
            raise RedisConfigurationError(
                "ENABLE_REDIS_ADAPTER=true requires Redis TCP or Upstash REST credentials"
            )
        self._validate_redis_url(settings.redis_url)
        if settings.retry_base_seconds > settings.retry_cap_seconds:
            raise RedisConfigurationError(
                "REDIS_RETRY_BASE_SECONDS cannot exceed retry cap"
            )
        return settings
