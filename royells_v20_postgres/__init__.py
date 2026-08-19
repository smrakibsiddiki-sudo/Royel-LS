"""Optional PostgreSQL/Neon infrastructure for Royells v20 Book 18."""

from .adapter import PostgresAdapter, PostgresResult
from .configuration import PostgresConfigurationProvider, PostgresSettings
from .errors import (
    ConnectionFailure,
    ForeignKeyViolation,
    IntegrityFailure,
    PostgresError,
    PostgresTimeout,
    SerializationFailure,
    UniqueViolation,
)
from .migrations import Migration, MigrationManager
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
    RuntimeRepository,
    SubscriptionRepository,
    TargetMediaRepository,
    TargetRepository,
    WorkerRepository,
)

__all__ = [
    "PostgresAdapter",
    "PostgresResult",
    "PostgresConfigurationProvider",
    "PostgresSettings",
    "ConnectionFailure",
    "ForeignKeyViolation",
    "IntegrityFailure",
    "PostgresError",
    "PostgresTimeout",
    "SerializationFailure",
    "UniqueViolation",
    "Migration",
    "MigrationManager",
    "AuditRepository",
    "ChannelRepository",
    "CheckpointRepository",
    "CursorRepository",
    "DeadMediaRepository",
    "JobRepository",
    "MetricsRepository",
    "PostedRepository",
    "QueueStateRepository",
    "RuntimeRepository",
    "SubscriptionRepository",
    "TargetMediaRepository",
    "TargetRepository",
    "WorkerRepository",
]
