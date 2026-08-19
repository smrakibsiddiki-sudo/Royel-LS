"""Validated, default-disabled PostgreSQL/Neon settings."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from royells_v20_core.configuration.environment import EnvironmentReader
from royells_v20_core.errors import ConfigurationError


@dataclass(frozen=True)
class PostgresSettings:
    """Connection, pool, timeout, and safety policy."""

    enabled: bool
    database_url: str
    production_selected: bool = False
    sslmode: str = "require"
    pool_min: int = 1
    pool_max: int = 5
    pool_timeout_seconds: float = 30.0
    pool_max_idle_seconds: float = 300.0
    pool_max_lifetime_seconds: float = 1800.0
    connect_timeout_seconds: float = 15.0
    statement_timeout_ms: int = 120000
    retry_attempts: int = 3
    retry_base_seconds: float = 0.5
    retry_cap_seconds: float = 10.0
    batch_size: int = 500
    allow_restore: bool = False
    application_name: str = "royells-v20-book18"

    def redacted_url(self) -> str:
        """Return a log-safe URL without credentials."""

        if "@" not in self.database_url:
            return "<configured>" if self.database_url else "<disabled>"
        prefix, suffix = self.database_url.rsplit("@", 1)
        scheme = prefix.split(":", 1)[0]
        return f"{scheme}://***:***@{suffix}"


class PostgresConfigurationProvider:
    """Read PostgreSQL settings without changing Book 17 defaults."""

    SSL_MODES = frozenset(
        {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
    )

    def __init__(self, environ=None) -> None:
        self._reader = EnvironmentReader(environ)

    def _database_url(self, sslmode: str) -> str:
        direct = self._reader.string("DATABASE_URL", "")
        if direct:
            return direct
        host = self._reader.string("POSTGRES_HOST", "")
        database = self._reader.string("POSTGRES_DB", "")
        user = self._reader.string("POSTGRES_USER", "")
        password = self._reader.string("POSTGRES_PASSWORD", "")
        port = self._reader.integer(
            "POSTGRES_PORT", 5432, minimum=1, maximum=65535
        )
        if not any((host, database, user, password)):
            return ""
        missing = [
            name
            for name, value in (
                ("POSTGRES_HOST", host),
                ("POSTGRES_DB", database),
                ("POSTGRES_USER", user),
                ("POSTGRES_PASSWORD", password),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Incomplete PostgreSQL configuration: " + ", ".join(missing)
            )
        return (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{quote(database, safe='')}"
            f"?sslmode={quote(sslmode, safe='-')}"
        )

    def settings(self) -> PostgresSettings:
        sslmode = self._reader.string("POSTGRES_SSLMODE", "require").lower()
        if sslmode not in self.SSL_MODES:
            raise ConfigurationError(f"Unsupported POSTGRES_SSLMODE: {sslmode}")
        settings = PostgresSettings(
            enabled=self._reader.boolean("ENABLE_POSTGRES_ADAPTER", False),
            database_url=self._database_url(sslmode),
            production_selected=self._reader.boolean("USE_POSTGRES", False),
            sslmode=sslmode,
            pool_min=self._reader.integer("POOL_MIN", 1, minimum=0, maximum=100),
            pool_max=self._reader.integer("POOL_MAX", 5, minimum=1, maximum=100),
            pool_timeout_seconds=self._reader.floating(
                "POOL_TIMEOUT", 30.0, minimum=0.1
            ),
            pool_max_idle_seconds=self._reader.floating(
                "POOL_MAX_IDLE", 300.0, minimum=1.0
            ),
            pool_max_lifetime_seconds=self._reader.floating(
                "POOL_MAX_LIFETIME", 1800.0, minimum=60.0
            ),
            connect_timeout_seconds=self._reader.floating(
                "POSTGRES_CONNECT_TIMEOUT", 15.0, minimum=1.0
            ),
            statement_timeout_ms=self._reader.integer(
                "POSTGRES_STATEMENT_TIMEOUT_MS",
                120000,
                minimum=1000,
            ),
            retry_attempts=self._reader.integer(
                "POSTGRES_RETRY_ATTEMPTS", 3, minimum=1, maximum=10
            ),
            retry_base_seconds=self._reader.floating(
                "POSTGRES_RETRY_BASE_SECONDS", 0.5, minimum=0.0
            ),
            retry_cap_seconds=self._reader.floating(
                "POSTGRES_RETRY_CAP_SECONDS", 10.0, minimum=0.1
            ),
            batch_size=self._reader.integer(
                "POSTGRES_BATCH_SIZE", 500, minimum=1, maximum=10000
            ),
            allow_restore=self._reader.boolean(
                "POSTGRES_ALLOW_LOGICAL_RESTORE", False
            ),
            application_name=self._reader.string(
                "POSTGRES_APPLICATION_NAME", "royells-v20-book18"
            ),
        )
        if settings.pool_min > settings.pool_max:
            raise ConfigurationError("POOL_MIN cannot exceed POOL_MAX")
        if settings.retry_base_seconds > settings.retry_cap_seconds:
            raise ConfigurationError(
                "POSTGRES_RETRY_BASE_SECONDS cannot exceed retry cap"
            )
        if settings.production_selected:
            raise ConfigurationError(
                "USE_POSTGRES=true is reserved for the approved Book 20 "
                "cutover; ENABLE_POSTGRES_ADAPTER may be used only for "
                "infrastructure validation"
            )
        if settings.enabled and not settings.database_url:
            raise ConfigurationError(
                "ENABLE_POSTGRES_ADAPTER=true requires DATABASE_URL or complete POSTGRES_* values"
            )
        return settings
