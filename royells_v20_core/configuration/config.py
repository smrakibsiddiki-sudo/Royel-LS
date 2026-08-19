"""ConfigurationProvider implementation backed by EnvironmentReader."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from ..errors import ConfigurationError
from ..interfaces import ConfigurationProvider
from ..models import (
    AuthorityPolicy,
    DatabaseAuthority,
    QueueAuthority,
    RedisRole,
    RuntimeAuthority,
)
from .environment import EnvironmentReader
from .settings import Settings
from .validation import validate_settings


class EnvironmentConfigurationProvider(ConfigurationProvider):
    """Build typed settings from one isolated environment reader."""

    def __init__(self, environ: Optional[Mapping[str, str]] = None) -> None:
        self._reader = EnvironmentReader(environ)
        self._settings: Settings | None = None

    def get(self, name: str, default: Any = None) -> Any:
        return self._reader.raw(name, default)

    def require(self, name: str) -> Any:
        value = self._reader.raw(name)
        if value is None or str(value).strip() == "":
            raise ConfigurationError(f"Required configuration is missing: {name}")
        return value

    def settings(self) -> Settings:
        if self._settings is not None:
            return self._settings
        data_dir = Path(
            self._reader.string("ROYELLS_DATA_DIR", "/data/royells_media_bot")
        ).expanduser()
        runtime_dir = Path(
            self._reader.string(
                "ROYELLS_RUNTIME_DIR",
                str(data_dir / "runtime"),
            )
        ).expanduser()
        use_sqlite = self._reader.boolean("USE_SQLITE", True)
        use_postgres = self._reader.boolean("USE_POSTGRES", False)
        use_json_state = self._reader.boolean("USE_JSON_STATE", True)
        use_redis_state = self._reader.boolean("USE_REDIS_STATE", False)
        use_redis_queue = self._reader.boolean("USE_REDIS_QUEUE", False)
        enable_dual_write = self._reader.boolean("ENABLE_DUAL_WRITE", False)
        enable_dual_read = self._reader.boolean("ENABLE_DUAL_READ", False)
        postgres_adapter_enabled = self._reader.boolean(
            "ENABLE_POSTGRES_ADAPTER",
            use_postgres or enable_dual_write or enable_dual_read,
        )
        redis_adapter_enabled = self._reader.boolean(
            "ENABLE_REDIS_ADAPTER",
            use_redis_state,
        )
        database_authority = (
            DatabaseAuthority.POSTGRES
            if use_postgres and not use_sqlite
            else DatabaseAuthority.LEGACY_SQLITE
        )
        queue_authority = (
            QueueAuthority.POSTGRES
            if self._reader.string("ROYELLS_QUEUE_AUTHORITY", "").lower()
            == QueueAuthority.POSTGRES.value
            else QueueAuthority.LEGACY_COMPOSITE
        )
        runtime_authority = (
            RuntimeAuthority.POSTGRES
            if not use_json_state and use_postgres
            else RuntimeAuthority.LEGACY_JSON
        )
        checkpoint_authority = (
            RuntimeAuthority.POSTGRES
            if self._reader.string("ROYELLS_CHECKPOINT_AUTHORITY", "").lower()
            == RuntimeAuthority.POSTGRES.value
            else RuntimeAuthority.LEGACY_JSON
        )
        authority = AuthorityPolicy(
            migration_epoch=self._reader.string(
                "ROYELLS_MIGRATION_EPOCH", "legacy-v1"
            ),
            database=database_authority,
            queue=queue_authority,
            runtime=runtime_authority,
            checkpoint=checkpoint_authority,
            redis_role=(
                RedisRole.PROJECTION
                if redis_adapter_enabled
                else RedisRole.DISABLED
            ),
            dual_write=enable_dual_write,
            dual_read=enable_dual_read,
            rollback_enabled=self._reader.boolean("ENABLE_ROLLBACK", False),
        )
        settings = Settings(
            data_dir=data_dir,
            runtime_dir=runtime_dir,
            sqlite_path=runtime_dir / "royells.db",
            # Kept only for isolated adapter tests. Production uses the exact
            # legacy composite queue through LegacyQueueRegistry.
            queue_sqlite_path=runtime_dir / "v20_adapter_queue.db",
            authority=authority,
            use_sqlite=use_sqlite,
            use_postgres=use_postgres,
            use_json_state=use_json_state,
            use_redis_state=use_redis_state,
            use_sqlite_queue=self._reader.boolean("USE_SQLITE_QUEUE", True),
            use_redis_queue=use_redis_queue,
            use_sqlite_checkpoint=self._reader.boolean(
                "USE_SQLITE_CHECKPOINT", True
            ),
            use_redis_checkpoint=self._reader.boolean(
                "USE_REDIS_CHECKPOINT", False
            ),
            enable_dual_write=enable_dual_write,
            enable_dual_read=enable_dual_read,
            enable_migration_log=self._reader.boolean(
                "ENABLE_MIGRATION_LOG", False
            ),
            enable_rollback=authority.rollback_enabled,
            sqlite_timeout_seconds=self._reader.floating(
                "ROYELLS_DB_TIMEOUT_SECONDS",
                30.0,
                minimum=0.1,
            ),
            sqlite_journal_mode=self._reader.string(
                "ROYELLS_DB_JOURNAL_MODE", "DELETE"
            ).upper(),
            sqlite_synchronous=self._reader.string(
                "ROYELLS_DB_SYNCHRONOUS", "NORMAL"
            ).upper(),
            json_fsync=self._reader.boolean("ROYELLS_JSON_FSYNC", True),
            telegram_request_timeout_seconds=self._reader.floating(
                "ROYELLS_TELEGRAM_CALL_TIMEOUT_SECONDS",
                120.0,
                minimum=1.0,
            ),
            postgres_adapter_enabled=postgres_adapter_enabled,
            redis_adapter_enabled=redis_adapter_enabled,
            database_url=self._reader.string(
                "DATABASE_URL",
                self._reader.string("POSTGRES_URL", ""),
            ),
            redis_url=self._reader.string("REDIS_URL", ""),
            upstash_url=self._reader.string(
                "UPSTASH_URL",
                self._reader.string("UPSTASH_REDIS_REST_URL", ""),
            ),
            upstash_token=self._reader.string(
                "UPSTASH_TOKEN",
                self._reader.string("UPSTASH_REDIS_REST_TOKEN", ""),
            ),
            v20_enabled=self._reader.boolean("ROYELLS_V20_ENABLED", True),
            dependency_injection_enabled=self._reader.boolean(
                "ROYELLS_V20_DEPENDENCY_INJECTION", True
            ),
            worker_refactor_enabled=self._reader.boolean(
                "ROYELLS_V20_WORKER_REFACTOR", True
            ),
            health_readiness_enabled=self._reader.boolean(
                "ROYELLS_V20_READINESS", True
            ),
            migration_journal_enabled=self._reader.boolean(
                "ENABLE_MIGRATION_LOG", False
            ),
            migration_journal_path=runtime_dir / "migration_journal.json",
        )
        self._settings = validate_settings(settings)
        return self._settings
