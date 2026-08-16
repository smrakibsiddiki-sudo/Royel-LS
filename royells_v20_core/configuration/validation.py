"""Cross-setting validation rules."""

from __future__ import annotations

from ..errors import ConfigurationError
from ..models import (
    DatabaseAuthority,
    QueueAuthority,
    RedisRole,
    RuntimeAuthority,
)
from .settings import Settings


def validate_settings(settings: Settings) -> Settings:
    """Reject unsupported migration combinations before any adapter starts."""

    errors: list[str] = []
    authority = settings.authority
    if settings.use_redis_queue:
        errors.append(
            "USE_REDIS_QUEUE is unsafe: Redis is projection-only in Royells v20"
        )
    if settings.use_redis_state:
        errors.append(
            "USE_REDIS_STATE is unsafe: Redis is projection-only in Royells v20"
        )
    if settings.use_redis_checkpoint:
        errors.append(
            "USE_REDIS_CHECKPOINT is unsafe: Redis cannot be the only durable checkpoint"
        )
    if settings.use_postgres:
        errors.append(
            "USE_POSTGRES is not available before the approved Book 20 cutover; "
            "use ENABLE_POSTGRES_ADAPTER=true only for infrastructure-only validation"
        )
    if authority.queue not in {
        QueueAuthority.LEGACY_COMPOSITE,
        QueueAuthority.POSTGRES,
    }:
        errors.append("Unsupported queue authority")
    if authority.database is DatabaseAuthority.POSTGRES:
        errors.append(
            "USE_POSTGRES is not available before the approved Book 20 cutover; "
            "Book 18 PostgreSQL support is infrastructure-only"
        )
    if authority.queue is QueueAuthority.POSTGRES and authority.database is not DatabaseAuthority.POSTGRES:
        errors.append("PostgreSQL queue authority requires PostgreSQL database authority")
    if authority.queue is QueueAuthority.POSTGRES:
        errors.append(
            "PostgreSQL queue authority is not available before Book 20"
        )
    if authority.runtime is RuntimeAuthority.POSTGRES and authority.database is not DatabaseAuthority.POSTGRES:
        errors.append("PostgreSQL runtime authority requires PostgreSQL database authority")
    if authority.runtime is RuntimeAuthority.POSTGRES:
        errors.append(
            "PostgreSQL runtime authority is not available before Book 20"
        )
    if authority.checkpoint is RuntimeAuthority.POSTGRES and authority.database is not DatabaseAuthority.POSTGRES:
        errors.append("PostgreSQL checkpoint authority requires PostgreSQL database authority")
    if authority.checkpoint is RuntimeAuthority.POSTGRES:
        errors.append(
            "PostgreSQL checkpoint authority is not available before Book 20"
        )
    if authority.redis_role is RedisRole.PROJECTION and not settings.redis_adapter_enabled:
        errors.append("Redis projection requires ENABLE_REDIS_ADAPTER=true")
    if settings.redis_adapter_enabled and not (
        settings.redis_url or (settings.upstash_url and settings.upstash_token)
    ):
        errors.append("Redis adapter requires REDIS_URL or an Upstash REST URL/token pair")
    if bool(settings.upstash_url) != bool(settings.upstash_token):
        errors.append("UPSTASH_URL and UPSTASH_TOKEN must be configured together")
    if authority.dual_write:
        errors.append(
            "ENABLE_DUAL_WRITE is reserved for Book 20 and cannot change "
            "Book 19 production behavior"
        )
    if authority.dual_read and not authority.dual_write:
        errors.append("Dual read requires dual write to be enabled first")
    if authority.dual_read:
        errors.append(
            "ENABLE_DUAL_READ is reserved for Book 20 and cannot change "
            "Book 19 production behavior"
        )
    if not settings.use_sqlite and authority.database is DatabaseAuthority.LEGACY_SQLITE:
        errors.append("Legacy SQLite authority requires USE_SQLITE=true")
    if not settings.use_json_state and authority.runtime is RuntimeAuthority.LEGACY_JSON:
        errors.append("Legacy JSON runtime authority requires USE_JSON_STATE=true")
    if not authority.migration_epoch.strip():
        errors.append("ROYELLS_MIGRATION_EPOCH cannot be empty")
    if settings.sqlite_timeout_seconds <= 0:
        errors.append("SQLite timeout must be positive")
    if settings.sqlite_journal_mode not in {
        "DELETE",
        "TRUNCATE",
        "PERSIST",
        "MEMORY",
        "WAL",
        "OFF",
    }:
        errors.append("Unsupported SQLite journal mode")
    if settings.sqlite_synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
        errors.append("Unsupported SQLite synchronous mode")
    if errors:
        raise ConfigurationError("; ".join(errors))
    return settings
