"""Production dependency-injection composition root."""

from __future__ import annotations

import contextlib
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from .adapters import (
    InMemoryMetrics,
    JsonRuntimeAdapter,
    LocalStorageAdapter,
    SQLiteAdapter,
    SQLiteQueueAdapter,
    StdlibLoggerAdapter,
    TelegramPyrogramAdapter,
)
from .adapters.legacy_bot import (
    LegacyCheckpointAdapter,
    LegacyDatabaseAdapter,
    LegacyDeliveryIntentRepository,
    LegacyLoggerAdapter,
    LegacyMetricsAdapter,
    LegacyQueueRegistry,
    LegacyRecoveryAdapter,
    LegacyRuntimeAdapter,
)
from .authority import StaticAuthorityPolicy
from .configuration import EnvironmentConfigurationProvider, Settings
from .downloads import (
    BotApiDownloadAdapter,
    DownloadRouter,
    RetryDownloadAdapter,
    UserbotDownloadAdapter,
)
from .errors import ServiceResolutionError
from .health import LegacyHealthService
from .interfaces import (
    AuthorityPolicyInterface,
    CheckpointInterface,
    ConfigurationProvider,
    DatabaseInterface,
    DeliveryIntentInterface,
    HealthInterface,
    LifecycleInterface,
    LoggerInterface,
    MetricsInterface,
    MigrationJournalInterface,
    QueueInterface,
    QueueRegistryInterface,
    RecoveryInterface,
    RuntimeStateInterface,
    StorageInterface,
    TelegramInterface,
)
from .lifecycle import ApplicationLifecycle
from .migration import DualWriteCoordinator, JsonMigrationJournal
from .repositories import (
    AuditRepository,
    ChannelRepository,
    CheckpointRepository,
    CursorRepository,
    DeadMediaRepository,
    JobRepository,
    MetricsRepository,
    PostedRepository,
    QueueStateRepository,
    SubscriptionRepository,
    TargetMediaRepository,
    WorkerRepository,
)
from .worker_contracts import WorkerDependencies
from .workers import TaskSupervisor


POSTGRES_ADAPTER = "royells.postgres.adapter"
REDIS_ADAPTER = "royells.redis.adapter"
REDIS_RUNTIME_ADAPTER = "royells.redis.runtime"
LEGACY_BRIDGE = "royells.legacy.bridge"


class ServiceContainer:
    """Lazy singleton container with deterministic resource shutdown."""

    def __init__(self) -> None:
        self._factories: dict[Any, Callable[[], Any]] = {}
        self._instances: dict[Any, Any] = {}
        self._creation_order: list[Any] = []
        self._closed = False

    def register_instance(self, key: Any, instance: Any) -> None:
        self._assert_registration(key)
        self._instances[key] = instance
        self._creation_order.append(key)

    def register_factory(self, key: Any, factory: Callable[[], Any]) -> None:
        self._assert_registration(key)
        self._factories[key] = factory

    def _assert_registration(self, key: Any) -> None:
        if self._closed:
            raise ServiceResolutionError("Service container is closed")
        if key in self._factories or key in self._instances:
            raise ServiceResolutionError(f"Service already registered: {key!r}")

    def registered(self, key: Any) -> bool:
        return key in self._factories or key in self._instances

    def resolve(self, key: Any) -> Any:
        if self._closed:
            raise ServiceResolutionError("Service container is closed")
        if key in self._instances:
            return self._instances[key]
        factory = self._factories.get(key)
        if factory is None:
            raise ServiceResolutionError(f"Service is not registered: {key!r}")
        instance = factory()
        self._instances[key] = instance
        self._creation_order.append(key)
        return instance

    def resolve_optional(self, key: Any, default: Any = None) -> Any:
        return self.resolve(key) if self.registered(key) else default

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        for key in reversed(self._creation_order):
            instance = self._instances.get(key)
            if instance is None or id(instance) in seen:
                continue
            seen.add(id(instance))
            closer = getattr(instance, "close", None)
            if closer is None:
                closer = getattr(instance, "disconnect", None)
            if closer is None:
                continue
            with contextlib.suppress(Exception):
                result = closer()
                if inspect.isawaitable(result):
                    await result
        self._instances.clear()
        self._factories.clear()
        self._creation_order.clear()


@dataclass(frozen=True)
class LegacyServiceBundle:
    """Original Book 17 test bundle retained for backward compatibility."""

    settings: Settings
    database: DatabaseInterface
    queue: QueueInterface
    runtime: RuntimeStateInterface
    storage: StorageInterface
    telegram: TelegramInterface | None
    metrics: MetricsInterface
    logger: LoggerInterface
    posted: PostedRepository
    target_media: TargetMediaRepository
    channels: ChannelRepository
    subscriptions: SubscriptionRepository
    dead_media: DeadMediaRepository
    metrics_repository: MetricsRepository


@dataclass(frozen=True)
class ProductionServiceBundle:
    """Resolved services used by the active compatibility runtime."""

    settings: Settings
    database: DatabaseInterface
    queues: QueueRegistryInterface
    runtime: RuntimeStateInterface
    checkpoint: CheckpointInterface
    recovery: RecoveryInterface
    lifecycle: LifecycleInterface
    delivery_intents: DeliveryIntentInterface
    storage: StorageInterface
    telegram: TelegramInterface | None
    download_router: DownloadRouter | None
    metrics: MetricsInterface
    logger: LoggerInterface
    authority: AuthorityPolicyInterface
    migration_journal: MigrationJournalInterface
    workers: TaskSupervisor
    health: HealthInterface
    posted: PostedRepository
    target_media: TargetMediaRepository
    channels: ChannelRepository
    subscriptions: SubscriptionRepository
    dead_media: DeadMediaRepository
    jobs: JobRepository
    queue_state: QueueStateRepository
    cursors: CursorRepository
    worker_state: WorkerRepository
    checkpoints: CheckpointRepository
    audit: AuditRepository
    metrics_repository: MetricsRepository


def build_legacy_container(
    configuration: ConfigurationProvider | None = None,
    *,
    telegram_client: Any = None,
) -> ServiceContainer:
    """Build the isolated Book 17 adapters used by their original tests."""

    config = configuration or EnvironmentConfigurationProvider()
    settings = config.settings()
    container = ServiceContainer()
    container.register_instance(ConfigurationProvider, config)
    container.register_instance(Settings, settings)
    container.register_factory(
        DatabaseInterface,
        lambda: SQLiteAdapter(
            settings.sqlite_path,
            timeout=settings.sqlite_timeout_seconds,
            journal_mode=settings.sqlite_journal_mode,
            synchronous=settings.sqlite_synchronous,
        ),
    )
    container.register_factory(
        QueueInterface,
        lambda: SQLiteQueueAdapter(
            settings.queue_sqlite_path,
            queue_name="royells-isolated",
            timeout=settings.sqlite_timeout_seconds,
        ),
    )
    container.register_factory(
        RuntimeStateInterface,
        lambda: JsonRuntimeAdapter(
            settings.runtime_dir / ".book17_state",
            fsync=settings.json_fsync,
        ),
    )
    container.register_factory(
        StorageInterface,
        lambda: LocalStorageAdapter(
            settings.data_dir,
            settings.runtime_dir / "downloads",
        ),
    )
    container.register_factory(MetricsInterface, InMemoryMetrics)
    container.register_factory(LoggerInterface, StdlibLoggerAdapter)
    if telegram_client is not None:
        container.register_factory(
            TelegramInterface,
            lambda: TelegramPyrogramAdapter(
                telegram_client,
                request_timeout=settings.telegram_request_timeout_seconds,
            ),
        )
    return container


def resolve_legacy_bundle(container: ServiceContainer) -> LegacyServiceBundle:
    database = container.resolve(DatabaseInterface)
    return LegacyServiceBundle(
        settings=container.resolve(Settings),
        database=database,
        queue=container.resolve(QueueInterface),
        runtime=container.resolve(RuntimeStateInterface),
        storage=container.resolve(StorageInterface),
        telegram=container.resolve_optional(TelegramInterface),
        metrics=container.resolve(MetricsInterface),
        logger=container.resolve(LoggerInterface),
        posted=PostedRepository(database),
        target_media=TargetMediaRepository(database),
        channels=ChannelRepository(database),
        subscriptions=SubscriptionRepository(database),
        dead_media=DeadMediaRepository(database),
        metrics_repository=MetricsRepository(database),
    )


def _register_optional_backends(
    container: ServiceContainer,
    settings: Settings,
) -> None:
    """Register PostgreSQL and Redis lazily without selecting authority."""

    if settings.postgres_adapter_enabled:
        from royells_v20_postgres import PostgresAdapter
        from royells_v20_postgres.configuration import PostgresSettings

        postgres_settings = PostgresSettings(
            enabled=True,
            database_url=settings.database_url,
            production_selected=(
                settings.authority.database.value == "postgres"
            ),
            application_name="royells-v20",
        )
        container.register_factory(
            POSTGRES_ADAPTER,
            lambda: PostgresAdapter(postgres_settings),
        )

    if settings.redis_adapter_enabled:
        from royells_v20_redis import (
            RedisAdapter,
            RedisRuntimeAdapter,
            UpstashRestAdapter,
        )
        from royells_v20_redis.configuration import RedisSettings

        redis_settings = RedisSettings(
            enabled=True,
            redis_url=settings.redis_url,
            production_queue_selected=False,
            production_runtime_selected=False,
            upstash_rest_url=settings.upstash_url,
            upstash_rest_token=settings.upstash_token,
        )
        if settings.redis_url:
            container.register_factory(
                REDIS_ADAPTER,
                lambda: RedisAdapter(redis_settings),
            )
            container.register_factory(
                REDIS_RUNTIME_ADAPTER,
                lambda: RedisRuntimeAdapter(redis_settings),
            )
        else:
            container.register_factory(
                REDIS_ADAPTER,
                lambda: UpstashRestAdapter(redis_settings),
            )


def build_production_container(
    bridge: Any,
    configuration: ConfigurationProvider | None = None,
    *,
    telegram_client: Any = None,
    bot_telegram_client: Any = None,
    download_call: Callable[..., Any] | None = None,
    download_retryable_error: Callable[[BaseException], bool] | None = None,
    download_permanent_error: Callable[[BaseException], bool] | None = None,
) -> ServiceContainer:
    """Build the active v20 composition root with legacy-safe authority."""

    config = configuration or EnvironmentConfigurationProvider()
    settings = config.settings()
    container = ServiceContainer()
    container.register_instance(LEGACY_BRIDGE, bridge)
    container.register_instance(ConfigurationProvider, config)
    container.register_instance(Settings, settings)
    container.register_instance(
        AuthorityPolicyInterface,
        StaticAuthorityPolicy(settings.authority),
    )
    container.register_instance(LifecycleInterface, ApplicationLifecycle())
    container.register_factory(
        DatabaseInterface,
        lambda: LegacyDatabaseAdapter(bridge),
    )
    container.register_factory(
        QueueRegistryInterface,
        lambda: LegacyQueueRegistry(bridge),
    )
    container.register_factory(
        QueueInterface,
        lambda: container.resolve(QueueRegistryInterface).get("download"),
    )
    container.register_factory(
        RuntimeStateInterface,
        lambda: LegacyRuntimeAdapter(bridge, settings.runtime_dir),
    )
    container.register_factory(
        CheckpointInterface,
        lambda: LegacyCheckpointAdapter(bridge),
    )
    container.register_factory(
        RecoveryInterface,
        lambda: LegacyRecoveryAdapter(bridge),
    )
    container.register_factory(
        DeliveryIntentInterface,
        lambda: LegacyDeliveryIntentRepository(bridge),
    )
    container.register_factory(
        StorageInterface,
        lambda: LocalStorageAdapter(
            settings.data_dir,
            settings.runtime_dir / "downloads",
        ),
    )
    container.register_factory(
        MetricsInterface,
        lambda: LegacyMetricsAdapter(bridge),
    )
    container.register_factory(
        LoggerInterface,
        lambda: LegacyLoggerAdapter(bridge),
    )
    if telegram_client is not None:
        container.register_factory(
            TelegramInterface,
            lambda: TelegramPyrogramAdapter(
                telegram_client,
                request_timeout=settings.telegram_request_timeout_seconds,
            ),
        )
    if (
        telegram_client is not None
        and bot_telegram_client is not None
        and download_call is not None
    ):
        retryable_error = download_retryable_error or (
            lambda error: any(
                marker in f"{type(error).__name__} {error}".lower()
                for marker in (
                    "timeout",
                    "timed out",
                    "network",
                    "connection",
                    "floodwait",
                    "flood_wait",
                    "temporarily",
                )
            )
        )
        permanent_error = download_permanent_error or (
            lambda error: any(
                marker in f"{type(error).__name__} {error}".lower()
                for marker in (
                    "media_empty",
                    "media empty",
                    "0-byte",
                    "invalid media",
                    "file must be non-empty",
                )
            )
        )
        container.register_factory(
            DownloadRouter,
            lambda: DownloadRouter(
                BotApiDownloadAdapter(
                    bot_telegram_client,
                    call=download_call,
                    retryable_error=retryable_error,
                    permanent_error=permanent_error,
                ),
                UserbotDownloadAdapter(
                    telegram_client,
                    call=download_call,
                    retryable_error=retryable_error,
                    permanent_error=permanent_error,
                ),
                RetryDownloadAdapter(
                    retryable_error=retryable_error,
                    permanent_error=permanent_error,
                ),
                logger=lambda message: container.resolve(LoggerInterface).info(
                    message
                ),
            ),
        )
    container.register_factory(
        MigrationJournalInterface,
        lambda: JsonMigrationJournal(
            settings.migration_journal_path
            or settings.runtime_dir / "migration_journal.json",
            fsync=settings.json_fsync,
        ),
    )
    container.register_factory(
        DualWriteCoordinator,
        lambda: DualWriteCoordinator(
            container.resolve(MigrationJournalInterface),
            enabled=settings.enable_dual_write,
        ),
    )
    _register_optional_backends(container, settings)

    database = container.resolve(DatabaseInterface)
    runtime = container.resolve(RuntimeStateInterface)
    repositories: dict[Any, Any] = {
        PostedRepository: PostedRepository(database),
        TargetMediaRepository: TargetMediaRepository(database),
        ChannelRepository: ChannelRepository(database),
        SubscriptionRepository: SubscriptionRepository(database),
        DeadMediaRepository: DeadMediaRepository(database),
        MetricsRepository: MetricsRepository(database),
        JobRepository: JobRepository(database),
        QueueStateRepository: QueueStateRepository(runtime),
        CursorRepository: CursorRepository(runtime),
        WorkerRepository: WorkerRepository(runtime),
        CheckpointRepository: CheckpointRepository(runtime),
        AuditRepository: AuditRepository(database),
    }
    for key, repository in repositories.items():
        container.register_instance(key, repository)

    dependencies = WorkerDependencies(
        queues=container.resolve(QueueRegistryInterface),
        storage=container.resolve(StorageInterface),
        telegram=container.resolve_optional(TelegramInterface),
        runtime=runtime,
        checkpoint=container.resolve(CheckpointInterface),
        recovery=container.resolve(RecoveryInterface),
        lifecycle=container.resolve(LifecycleInterface),
        authority=container.resolve(AuthorityPolicyInterface),
        delivery_intents=container.resolve(DeliveryIntentInterface),
        posted=repositories[PostedRepository],
        target_media=repositories[TargetMediaRepository],
        channels=repositories[ChannelRepository],
        subscriptions=repositories[SubscriptionRepository],
        dead_media=repositories[DeadMediaRepository],
        metrics_repository=repositories[MetricsRepository],
        metrics=container.resolve(MetricsInterface),
        logger=container.resolve(LoggerInterface),
    )
    container.register_instance(WorkerDependencies, dependencies)
    supervisor = TaskSupervisor(dependencies)
    container.register_instance(TaskSupervisor, supervisor)
    container.register_factory(
        HealthInterface,
        lambda: LegacyHealthService(
            bridge,
            settings,
            container.resolve(AuthorityPolicyInterface),
            container.resolve(CheckpointInterface),
            container.resolve(RecoveryInterface),
            container.resolve(LifecycleInterface),
            container.resolve(MetricsInterface),
            worker_supervisor=supervisor,
        ),
    )
    return container


def resolve_production_bundle(container: ServiceContainer) -> ProductionServiceBundle:
    database = container.resolve(DatabaseInterface)
    return ProductionServiceBundle(
        settings=container.resolve(Settings),
        database=database,
        queues=container.resolve(QueueRegistryInterface),
        runtime=container.resolve(RuntimeStateInterface),
        checkpoint=container.resolve(CheckpointInterface),
        recovery=container.resolve(RecoveryInterface),
        lifecycle=container.resolve(LifecycleInterface),
        delivery_intents=container.resolve(DeliveryIntentInterface),
        storage=container.resolve(StorageInterface),
        telegram=container.resolve_optional(TelegramInterface),
        download_router=container.resolve_optional(DownloadRouter),
        metrics=container.resolve(MetricsInterface),
        logger=container.resolve(LoggerInterface),
        authority=container.resolve(AuthorityPolicyInterface),
        migration_journal=container.resolve(MigrationJournalInterface),
        workers=container.resolve(TaskSupervisor),
        health=container.resolve(HealthInterface),
        posted=container.resolve(PostedRepository),
        target_media=container.resolve(TargetMediaRepository),
        channels=container.resolve(ChannelRepository),
        subscriptions=container.resolve(SubscriptionRepository),
        dead_media=container.resolve(DeadMediaRepository),
        jobs=container.resolve(JobRepository),
        queue_state=container.resolve(QueueStateRepository),
        cursors=container.resolve(CursorRepository),
        worker_state=container.resolve(WorkerRepository),
        checkpoints=container.resolve(CheckpointRepository),
        audit=container.resolve(AuditRepository),
        metrics_repository=container.resolve(MetricsRepository),
    )
