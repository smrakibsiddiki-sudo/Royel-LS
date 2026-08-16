"""Immutable Royells v20 settings with legacy-safe defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import AuthorityPolicy


@dataclass(frozen=True)
class Settings:
    """Validated configuration consumed by the service container."""

    data_dir: Path
    runtime_dir: Path
    sqlite_path: Path
    queue_sqlite_path: Path
    authority: AuthorityPolicy = AuthorityPolicy()
    use_sqlite: bool = True
    use_postgres: bool = False
    use_json_state: bool = True
    use_redis_state: bool = False
    use_sqlite_queue: bool = True
    use_redis_queue: bool = False
    use_sqlite_checkpoint: bool = True
    use_redis_checkpoint: bool = False
    enable_dual_write: bool = False
    enable_dual_read: bool = False
    enable_migration_log: bool = False
    enable_rollback: bool = False
    sqlite_timeout_seconds: float = 30.0
    sqlite_journal_mode: str = "DELETE"
    sqlite_synchronous: str = "NORMAL"
    json_fsync: bool = True
    telegram_request_timeout_seconds: float = 120.0
    postgres_adapter_enabled: bool = False
    redis_adapter_enabled: bool = False
    database_url: str = ""
    redis_url: str = ""
    upstash_url: str = ""
    upstash_token: str = ""
    v20_enabled: bool = True
    dependency_injection_enabled: bool = True
    worker_refactor_enabled: bool = True
    health_readiness_enabled: bool = True
    migration_journal_enabled: bool = False
    migration_journal_path: Path | None = None

    def redacted(self) -> dict[str, object]:
        """Return configuration suitable for health and diagnostic output."""

        return {
            "data_dir": str(self.data_dir),
            "runtime_dir": str(self.runtime_dir),
            "sqlite_path": str(self.sqlite_path),
            "authority": {
                "migration_epoch": self.authority.migration_epoch,
                "database": self.authority.database.value,
                "queue": self.authority.queue.value,
                "runtime": self.authority.runtime.value,
                "checkpoint": self.authority.checkpoint.value,
                "redis_role": self.authority.redis_role.value,
                "dual_write": self.authority.dual_write,
                "dual_read": self.authority.dual_read,
            },
            "postgres_configured": bool(self.database_url),
            "redis_configured": bool(self.redis_url or (self.upstash_url and self.upstash_token)),
            "v20_enabled": self.v20_enabled,
            "dependency_injection_enabled": self.dependency_injection_enabled,
            "worker_refactor_enabled": self.worker_refactor_enabled,
        }
