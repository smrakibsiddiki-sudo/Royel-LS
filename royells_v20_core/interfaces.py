"""Technology-neutral interfaces for Book 17.

The interfaces deliberately avoid sqlite3, Pyrogram, Redis, PostgreSQL, and
filesystem implementation details.  Concrete adapters own those details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from os import PathLike
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

from .models import (
    AuthorityPolicy,
    CheckpointRecord,
    DeliveryIntentRecord,
    QueueItem,
    RecoveryReport,
    ValidationMismatch,
)


class DatabaseInterface(ABC):
    """Transaction-oriented database contract."""

    @abstractmethod
    def connect(self) -> None:
        """Open the database connection."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release the active connection."""

    @abstractmethod
    def begin(self) -> None:
        """Begin a transaction."""

    @abstractmethod
    def commit(self) -> None:
        """Commit the current transaction."""

    @abstractmethod
    def rollback(self) -> None:
        """Roll back the current transaction."""

    @abstractmethod
    def transaction(self) -> AbstractContextManager["DatabaseInterface"]:
        """Return a context manager that commits or rolls back atomically."""

    @abstractmethod
    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        """Execute a parameterized statement."""

    @abstractmethod
    def executemany(
        self, statement: str, parameter_sets: Sequence[Sequence[Any]]
    ) -> Any:
        """Execute a parameterized statement for many rows."""

    @abstractmethod
    def fetchone(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        """Fetch one row."""

    @abstractmethod
    def fetchall(self, statement: str, parameters: Sequence[Any] = ()) -> list[Any]:
        """Fetch all rows."""

    @abstractmethod
    def insert(self, table: str, values: Mapping[str, Any]) -> int:
        """Insert a row using validated identifiers and bound values."""

    @abstractmethod
    def update(
        self,
        table: str,
        values: Mapping[str, Any],
        where: Mapping[str, Any],
    ) -> int:
        """Update rows using validated identifiers and bound values."""

    @abstractmethod
    def delete(self, table: str, where: Mapping[str, Any]) -> int:
        """Delete rows using validated identifiers and bound values."""

    @abstractmethod
    def health(self) -> Mapping[str, Any]:
        """Return an integrity and connectivity snapshot."""

    @abstractmethod
    def ping(self) -> float:
        """Return database round-trip latency in seconds."""

    @abstractmethod
    def backup(self, destination: PathLike[str] | str) -> None:
        """Create a crash-safe database backup."""

    @abstractmethod
    def restore(self, source: PathLike[str] | str) -> None:
        """Restore a validated database backup."""

    @abstractmethod
    def close(self) -> None:
        """Close the adapter and release all resources."""


class QueueInterface(ABC):
    """Durable queue contract with explicit acknowledgement and leases."""

    @abstractmethod
    async def enqueue(
        self,
        payload: Mapping[str, Any],
        *,
        item_id: Optional[str] = None,
        priority: int = 0,
        available_at: Optional[float] = None,
    ) -> str:
        """Add an idempotent queue item and return its stable id."""

    @abstractmethod
    async def dequeue(
        self,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
        timeout: Optional[float] = None,
    ) -> Optional[QueueItem]:
        """Reserve the next available item for a worker."""

    @abstractmethod
    async def peek(self) -> Optional[QueueItem]:
        """Read the next available item without reserving it."""

    @abstractmethod
    async def ack(self, item_id: str, worker_id: Optional[str] = None) -> bool:
        """Acknowledge and remove a completed item."""

    @abstractmethod
    async def nack(
        self,
        item_id: str,
        *,
        worker_id: Optional[str] = None,
        delay_seconds: float = 0.0,
        error: str = "",
    ) -> bool:
        """Return an item to the pending state."""

    @abstractmethod
    async def retry(
        self,
        item_id: str,
        *,
        worker_id: Optional[str] = None,
        delay_seconds: float = 0.0,
        error: str = "",
    ) -> bool:
        """Schedule an item for a later attempt."""

    @abstractmethod
    async def size(self) -> int:
        """Return the number of unacknowledged items."""

    @abstractmethod
    async def clear(self) -> int:
        """Remove all items and return the number removed."""

    @abstractmethod
    async def reserve(
        self,
        item_id: str,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
    ) -> bool:
        """Atomically reserve a specific pending item."""

    @abstractmethod
    async def heartbeat(
        self,
        item_id: str,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
    ) -> bool:
        """Extend a worker lease."""

    @abstractmethod
    async def metrics(self) -> Mapping[str, Any]:
        """Return queue health metrics."""


class QueueRegistryInterface(ABC):
    """Resolve the named queue used by a worker or application service."""

    @abstractmethod
    def get(self, name: str) -> QueueInterface:
        """Return a registered queue by canonical name."""

    @abstractmethod
    def names(self) -> Sequence[str]:
        """Return all canonical queue names."""

    @abstractmethod
    async def metrics(self) -> Mapping[str, Any]:
        """Return a snapshot for every registered queue."""


class RuntimeStateInterface(ABC):
    """Atomic named runtime state and short-lived ownership contract."""

    @abstractmethod
    def load(self, name: str = "runtime_checkpoint") -> Any:
        """Load and validate a named state document."""

    @abstractmethod
    def save(self, name: str, payload: Any) -> int:
        """Atomically persist a named state document and return its revision."""

    @abstractmethod
    def delete(self, name: str) -> None:
        """Delete a named state document."""

    @abstractmethod
    def checkpoint(self, payload: Any, name: str = "runtime_checkpoint") -> int:
        """Persist a checkpoint using the same atomic protocol as save."""

    @abstractmethod
    def restore(self, name: str = "runtime_checkpoint") -> Any:
        """Restore a checkpoint, including previous-good fallback."""

    @abstractmethod
    def heartbeat(
        self, component: str, metadata: Optional[Mapping[str, Any]] = None
    ) -> int:
        """Persist a component heartbeat."""

    @abstractmethod
    def lock(self, name: str = "runtime") -> str:
        """Acquire an ownership token for a named state scope."""

    @abstractmethod
    def unlock(self, token: str) -> None:
        """Release an ownership token."""


class CheckpointInterface(ABC):
    """Versioned durable checkpoint generations with integrity validation."""

    @abstractmethod
    def load(self, name: str = "runtime_checkpoint") -> Optional[CheckpointRecord]:
        """Load the newest valid checkpoint generation."""

    @abstractmethod
    async def capture(
        self,
        reason: str,
        *,
        force: bool = False,
        name: str = "runtime_checkpoint",
    ) -> CheckpointRecord:
        """Capture and persist one consistent runtime generation."""

    @abstractmethod
    def restore(self, name: str = "runtime_checkpoint") -> Optional[CheckpointRecord]:
        """Restore the newest compatible primary or previous generation."""

    @abstractmethod
    def validate(self, record: CheckpointRecord) -> bool:
        """Verify version, checksum, and required checkpoint invariants."""

    @abstractmethod
    def status(self) -> Mapping[str, Any]:
        """Return checkpoint health and last-generation metadata."""


class RecoveryInterface(ABC):
    """Crash-recovery planning, execution, and validation contract."""

    @abstractmethod
    async def recover(self, *, reason: str = "startup") -> RecoveryReport:
        """Reconcile unfinished jobs and return a durable recovery report."""

    @abstractmethod
    async def validate(self) -> RecoveryReport:
        """Validate queue, delivery, cursor, and checkpoint consistency."""

    @abstractmethod
    def status(self) -> Mapping[str, Any]:
        """Return the most recent recovery state."""


class AuthorityPolicyInterface(ABC):
    """Expose and enforce the current migration authority policy."""

    @abstractmethod
    def current(self) -> AuthorityPolicy:
        """Return the immutable current authority policy."""

    @abstractmethod
    def require_write_authority(self, domain: str, backend: str) -> None:
        """Raise when a backend is not allowed to mutate a domain."""

    @abstractmethod
    def snapshot(self) -> Mapping[str, Any]:
        """Return a redaction-safe authority snapshot."""


class MigrationJournalInterface(ABC):
    """Durable journal used by shadow, dual-write, validation, and rollback."""

    @abstractmethod
    def begin(self, operation: str, metadata: Optional[Mapping[str, Any]] = None) -> str:
        """Begin a migration operation and return its stable journal id."""

    @abstractmethod
    def checkpoint(
        self,
        journal_id: str,
        *,
        cursor: str,
        counts: Optional[Mapping[str, int]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Persist resumable migration progress."""

    @abstractmethod
    def mismatch(self, journal_id: str, mismatch: ValidationMismatch) -> None:
        """Persist one cross-backend mismatch."""

    @abstractmethod
    def complete(
        self,
        journal_id: str,
        *,
        status: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Complete, fail, or roll back a migration operation."""

    @abstractmethod
    def load(self, journal_id: str) -> Optional[Mapping[str, Any]]:
        """Load one journal entry."""

    @abstractmethod
    def list_open(self) -> list[Mapping[str, Any]]:
        """Return unfinished migration operations."""


class DeliveryIntentInterface(ABC):
    """Durable Telegram delivery intent and ambiguity contract."""

    @abstractmethod
    def create(self, intent: DeliveryIntentRecord) -> DeliveryIntentRecord:
        """Persist an idempotent intent before a Telegram request."""

    @abstractmethod
    def get(self, intent_id: str) -> Optional[DeliveryIntentRecord]:
        """Return one delivery intent."""

    @abstractmethod
    def pending(self, job_id: Optional[str] = None) -> Sequence[DeliveryIntentRecord]:
        """Return unresolved intents, optionally for one job."""

    @abstractmethod
    def mark_accepted(
        self,
        intent_id: str,
        target_message_ids: Iterable[int],
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> DeliveryIntentRecord:
        """Record Telegram acceptance evidence."""

    @abstractmethod
    def complete(self, intent_id: str) -> DeliveryIntentRecord:
        """Mark an accepted intent fully committed."""

    @abstractmethod
    def fail(self, intent_id: str, error: str) -> DeliveryIntentRecord:
        """Mark an intent failed without discarding ambiguity evidence."""


class HealthInterface(ABC):
    """Application liveness, readiness, startup, and dependency health."""

    @abstractmethod
    def liveness(self) -> Mapping[str, Any]:
        """Return process liveness."""

    @abstractmethod
    def readiness(self) -> Mapping[str, Any]:
        """Return whether production authority can safely serve work."""

    @abstractmethod
    def startup(self) -> Mapping[str, Any]:
        """Return boot and recovery progress."""

    @abstractmethod
    def snapshot(self) -> Mapping[str, Any]:
        """Return the combined health snapshot."""


class LifecycleInterface(ABC):
    """Application shutdown and resource-drain coordination."""

    @abstractmethod
    def request_shutdown(self, reason: str, exit_code: int = 0) -> None:
        """Request graceful shutdown without terminating the process directly."""

    @abstractmethod
    async def wait(self) -> tuple[str, int]:
        """Wait until shutdown is requested."""

    @abstractmethod
    def requested(self) -> bool:
        """Return whether shutdown has been requested."""

    @abstractmethod
    def status(self) -> Mapping[str, Any]:
        """Return lifecycle state."""


class StorageInterface(ABC):
    """Temporary media storage contract."""

    @abstractmethod
    def download(
        self,
        source: PathLike[str] | str,
        destination: Optional[PathLike[str] | str] = None,
    ) -> Any:
        """Materialize a source into local storage."""

    @abstractmethod
    def upload(
        self,
        source: PathLike[str] | str,
        destination: PathLike[str] | str,
    ) -> Any:
        """Materialize a local source at a destination."""

    @abstractmethod
    def delete(self, path: PathLike[str] | str) -> None:
        """Delete a temporary file."""

    @abstractmethod
    def exists(self, path: PathLike[str] | str) -> bool:
        """Return whether a path exists."""

    @abstractmethod
    def size(self, path: PathLike[str] | str) -> int:
        """Return a file size in bytes."""

    @abstractmethod
    def hash(self, path: PathLike[str] | str, algorithm: str = "sha256") -> str:
        """Return a content digest."""

    @abstractmethod
    def cleanup(
        self,
        path: Optional[PathLike[str] | str] = None,
        *,
        older_than_seconds: Optional[float] = None,
    ) -> int:
        """Remove temporary artifacts and return the removal count."""

    @abstractmethod
    def temp_path(self, suffix: str = "", prefix: str = "royells-") -> Any:
        """Return a unique temporary path owned by this adapter."""


class TelegramInterface(ABC):
    """Telegram transport contract."""

    @abstractmethod
    async def download_media(self, message: Any, **kwargs: Any) -> Any:
        """Download media from a Telegram message."""

    @abstractmethod
    async def upload_media(self, chat_id: Any, media: Any, **kwargs: Any) -> Any:
        """Upload one media item."""

    @abstractmethod
    async def copy_message(self, chat_id: Any, from_chat_id: Any, message_id: int, **kwargs: Any) -> Any:
        """Copy one message."""

    @abstractmethod
    async def copy_album(self, chat_id: Any, from_chat_id: Any, message_ids: Sequence[int], **kwargs: Any) -> Any:
        """Copy an album or media group."""

    @abstractmethod
    async def forward(self, chat_id: Any, from_chat_id: Any, message_ids: Sequence[int], **kwargs: Any) -> Any:
        """Forward one or more messages."""

    @abstractmethod
    async def send_album(self, chat_id: Any, media: Sequence[Any], **kwargs: Any) -> Any:
        """Send an album."""

    @abstractmethod
    async def resolve_peer(self, peer: Any) -> Any:
        """Resolve a Telegram peer."""

    @abstractmethod
    async def get_chat(self, chat_id: Any) -> Any:
        """Fetch a Telegram chat."""

    @abstractmethod
    async def retry(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        retries: int = 3,
        label: str = "telegram",
    ) -> Any:
        """Retry a bounded Telegram operation."""


class MetricsInterface(ABC):
    """Metrics collection contract."""

    @abstractmethod
    def increment(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        """Increment a counter."""

    @abstractmethod
    def decrement(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        """Decrement a counter."""

    @abstractmethod
    def observe(self, name: str, value: float, **labels: Any) -> None:
        """Record an observation."""

    @abstractmethod
    def gauge(self, name: str, value: float, **labels: Any) -> None:
        """Set a gauge."""

    @abstractmethod
    def timer(self, name: str, **labels: Any) -> AbstractContextManager[None]:
        """Return a context manager that observes elapsed seconds."""

    @abstractmethod
    def snapshot(self) -> Mapping[str, Any]:
        """Return a serializable metrics snapshot."""


class ConfigurationProvider(ABC):
    """Single source of configuration values."""

    @abstractmethod
    def get(self, name: str, default: Any = None) -> Any:
        """Read a configuration value."""

    @abstractmethod
    def require(self, name: str) -> Any:
        """Read a required value or raise ConfigurationError."""

    @abstractmethod
    def settings(self) -> Any:
        """Return validated typed settings."""


class LoggerInterface(ABC):
    """Structured logging contract."""

    @abstractmethod
    def info(self, message: str, **fields: Any) -> None:
        """Log an informational message."""

    @abstractmethod
    def warn(self, message: str, **fields: Any) -> None:
        """Log a warning."""

    @abstractmethod
    def error(self, message: str, **fields: Any) -> None:
        """Log an error."""

    @abstractmethod
    def performance(self, message: str, **fields: Any) -> None:
        """Log a performance event."""

    @abstractmethod
    def audit(self, message: str, **fields: Any) -> None:
        """Log an audit event."""
