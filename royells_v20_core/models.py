"""Versioned transport models shared by Royells v20 components."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class DatabaseAuthority(str, Enum):
    """Durable database authority selected for the current migration epoch."""

    LEGACY_SQLITE = "legacy_sqlite"
    POSTGRES = "postgres"


class QueueAuthority(str, Enum):
    """Durable queue authority.

    Redis is intentionally absent: Redis is a rebuildable projection and
    notification layer in the approved Royells v20 architecture.
    """

    LEGACY_COMPOSITE = "legacy_composite"
    POSTGRES = "postgres"


class RuntimeAuthority(str, Enum):
    """Durable runtime/checkpoint authority."""

    LEGACY_JSON = "legacy_json"
    POSTGRES = "postgres"


class RedisRole(str, Enum):
    """Redis participation mode."""

    DISABLED = "disabled"
    PROJECTION = "projection"


@dataclass(frozen=True)
class AuthorityPolicy:
    """Validated authority selection for one migration epoch."""

    migration_epoch: str = "legacy-v1"
    database: DatabaseAuthority = DatabaseAuthority.LEGACY_SQLITE
    queue: QueueAuthority = QueueAuthority.LEGACY_COMPOSITE
    runtime: RuntimeAuthority = RuntimeAuthority.LEGACY_JSON
    checkpoint: RuntimeAuthority = RuntimeAuthority.LEGACY_JSON
    redis_role: RedisRole = RedisRole.DISABLED
    dual_write: bool = False
    dual_read: bool = False
    rollback_enabled: bool = False

    @property
    def legacy_only(self) -> bool:
        """Return whether every production authority remains legacy."""

        return (
            self.database is DatabaseAuthority.LEGACY_SQLITE
            and self.queue is QueueAuthority.LEGACY_COMPOSITE
            and self.runtime is RuntimeAuthority.LEGACY_JSON
            and self.checkpoint is RuntimeAuthority.LEGACY_JSON
            and self.redis_role is RedisRole.DISABLED
            and not self.dual_write
            and not self.dual_read
        )


@dataclass(frozen=True)
class QueueItem:
    """A durable queue item and its current reservation metadata."""

    item_id: str
    payload: Mapping[str, Any]
    priority: int = 0
    attempts: int = 0
    status: str = "pending"
    reserved_by: Optional[str] = None
    lease_until: Optional[float] = None
    available_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""

        return asdict(self)


@dataclass(frozen=True)
class RuntimeEnvelope:
    """Validated metadata around a persisted runtime payload."""

    schema_version: int
    revision: int
    written_at: str
    payload: Any
    sha256: str


@dataclass(frozen=True)
class CheckpointRecord:
    """One validated durable checkpoint generation."""

    name: str
    schema_version: int
    revision: int
    written_at: str
    payload: Any
    sha256: str
    source: str = ""


@dataclass(frozen=True)
class RecoveryReport:
    """Backend-neutral result of one recovery pass."""

    recovered: int = 0
    deferred: int = 0
    duplicates: int = 0
    failed: int = 0
    pending: int = 0
    consistent: bool = True
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryIntentRecord:
    """Durable evidence written before a Telegram delivery attempt."""

    intent_id: str
    job_id: str
    operation: str
    status: str
    media_uids: Sequence[str] = ()
    target_chat_id: Optional[int] = None
    target_message_ids: Sequence[int] = ()
    attempt: int = 1
    created_at: str = ""
    updated_at: str = ""
    last_error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerState:
    """Observable worker lifecycle and ownership snapshot."""

    worker_id: str
    worker_type: str
    status: str
    heartbeat_at: float
    active_job_id: str = ""
    started_at: float = 0.0
    restart_count: int = 0
    last_error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationMismatch:
    """One dual-backend validation mismatch."""

    domain: str
    record_key: str
    mismatch_type: str
    legacy_hash: str = ""
    candidate_hash: str = ""
    detail: str = ""
    detected_at: str = ""
