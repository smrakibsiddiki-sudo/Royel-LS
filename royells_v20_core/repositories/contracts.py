"""Domain repository contracts consumed by application workers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional, Sequence


class PostedRepositoryInterface(ABC):
    @abstractmethod
    def exists(self, media_hash: str) -> bool:
        """Return whether a media hash has been posted."""

    @abstractmethod
    def add(self, media_hash: str, channel: str = "") -> None:
        """Idempotently record a posted hash."""

    @abstractmethod
    def remove(self, media_hash: str) -> bool:
        """Remove a posted hash."""


class TargetMediaRepositoryInterface(ABC):
    @abstractmethod
    def exists(self, uid: str) -> bool:
        """Return whether a target media UID exists."""

    @abstractmethod
    def upsert(
        self,
        uid: str,
        *,
        message_id: int = 0,
        indexed_at: str = "",
        source: str = "target_scan",
    ) -> None:
        """Idempotently store a target media UID and index metadata."""

    @abstractmethod
    def remove(self, uid: str) -> bool:
        """Remove a target media UID from both target tables."""


class ChannelRepositoryInterface(ABC):
    @abstractmethod
    def get(self, channel_id: str) -> Optional[Mapping[str, Any]]:
        """Return one source channel."""

    @abstractmethod
    def list_all(self) -> list[Mapping[str, Any]]:
        """Return all source channels."""

    @abstractmethod
    def upsert(self, channel: Mapping[str, Any]) -> None:
        """Idempotently store source channel metadata."""

    @abstractmethod
    def remove(self, channel_id: str) -> bool:
        """Remove one source channel."""


class SubscriptionRepositoryInterface(ABC):
    @abstractmethod
    def get(self, user_id: str) -> Optional[Mapping[str, Any]]:
        """Return one subscription."""

    @abstractmethod
    def upsert(self, subscription: Mapping[str, Any]) -> None:
        """Idempotently store subscription metadata."""

    @abstractmethod
    def remove(self, user_id: str) -> bool:
        """Remove one subscription."""


class DeadMediaRepositoryInterface(ABC):
    @abstractmethod
    def get(self, uid: str) -> Optional[Mapping[str, Any]]:
        """Return one dead-media record."""

    @abstractmethod
    def upsert(self, record: Mapping[str, Any]) -> None:
        """Idempotently store dead-media evidence."""

    @abstractmethod
    def remove(self, uid: str) -> bool:
        """Remove one dead-media record."""


class MetricsRepositoryInterface(ABC):
    @abstractmethod
    def record(
        self,
        name: str,
        value: float,
        *,
        recorded_at: str,
        labels: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Persist one metric observation."""

    @abstractmethod
    def latest(self, name: str, limit: int = 100) -> list[Mapping[str, Any]]:
        """Return recent observations for a metric."""


class JobRepositoryInterface(ABC):
    """Durable media job state."""

    @abstractmethod
    def get(self, job_id: str) -> Optional[Mapping[str, Any]]:
        """Return one job."""

    @abstractmethod
    def upsert(self, job: Mapping[str, Any]) -> None:
        """Idempotently persist one job."""

    @abstractmethod
    def list_unfinished(self, limit: int = 1000) -> list[Mapping[str, Any]]:
        """Return resumable unfinished jobs."""

    @abstractmethod
    def transition(
        self,
        job_id: str,
        *,
        stage: str,
        status: str,
        error: str = "",
    ) -> bool:
        """Persist one legal job transition."""


class QueueStateRepositoryInterface(ABC):
    """Durable queue snapshots and fairness state."""

    @abstractmethod
    def load(self, queue_name: str) -> Mapping[str, Any]:
        """Load one queue snapshot."""

    @abstractmethod
    def save(self, queue_name: str, state: Mapping[str, Any]) -> None:
        """Persist one queue snapshot."""

    @abstractmethod
    def list_pending(self, queue_name: str) -> Sequence[Mapping[str, Any]]:
        """Return unfinished queue records."""


class CursorRepositoryInterface(ABC):
    """Source scan cursor storage."""

    @abstractmethod
    def get(self, source_id: str, cursor_kind: str) -> Optional[Mapping[str, Any]]:
        """Return one source cursor."""

    @abstractmethod
    def upsert(
        self,
        source_id: str,
        cursor_kind: str,
        cursor: Mapping[str, Any],
    ) -> None:
        """Persist one versioned source cursor."""

    @abstractmethod
    def list_incomplete(self, cursor_kind: str) -> Sequence[Mapping[str, Any]]:
        """Return incomplete cursors of one kind."""


class WorkerRepositoryInterface(ABC):
    """Worker heartbeat and ownership state."""

    @abstractmethod
    def heartbeat(self, worker: Mapping[str, Any]) -> None:
        """Persist a worker heartbeat."""

    @abstractmethod
    def remove(self, worker_id: str) -> bool:
        """Remove a terminated worker."""

    @abstractmethod
    def list_live(self, max_age_seconds: float = 120.0) -> Sequence[Mapping[str, Any]]:
        """Return workers with a recent heartbeat."""


class CheckpointRepositoryInterface(ABC):
    """Repository-level access to durable checkpoint generations."""

    @abstractmethod
    def latest(self, name: str = "runtime_checkpoint") -> Optional[Mapping[str, Any]]:
        """Return the latest valid checkpoint metadata."""

    @abstractmethod
    def save(self, name: str, checkpoint: Mapping[str, Any]) -> None:
        """Persist a checkpoint generation."""


class AuditRepositoryInterface(ABC):
    """Append-only operational audit events."""

    @abstractmethod
    def append(
        self,
        action: str,
        *,
        object_type: str,
        object_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Append an audit event and return its id."""

    @abstractmethod
    def latest(self, limit: int = 100) -> Sequence[Mapping[str, Any]]:
        """Return recent audit events."""
