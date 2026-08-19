"""Canonical PostgreSQL repositories for the Royells v20 schema."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Mapping, Optional, Sequence

from royells_v20_core.errors import DatabaseError
from royells_v20_core.interfaces import DatabaseInterface
from royells_v20_core.repositories.contracts import (
    AuditRepositoryInterface,
    ChannelRepositoryInterface,
    CheckpointRepositoryInterface,
    CursorRepositoryInterface,
    DeadMediaRepositoryInterface,
    JobRepositoryInterface,
    MetricsRepositoryInterface,
    PostedRepositoryInterface,
    QueueStateRepositoryInterface,
    SubscriptionRepositoryInterface,
    TargetMediaRepositoryInterface,
    WorkerRepositoryInterface,
)


_QUEUE_NAMES = frozenset(
    {
        "download",
        "upload",
        "link",
        "retry",
        "admission",
        "album",
        "processing",
        "reconciliation",
        "db_critical",
        "db_job_state",
    }
)
_JOB_TYPES = frozenset(
    {"single", "album", "link", "reconciliation", "target_index"}
)
_JOB_STATUSES = frozenset(
    {
        "admitted",
        "queued",
        "downloading",
        "downloaded",
        "uploading",
        "reconciling",
        "retry_wait",
        "completed",
        "failed",
        "dead",
        "cancelled",
    }
)
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "dead", "cancelled"})
_SOURCE_KINDS = frozenset(
    {
        "source",
        "realtime",
        "hot",
        "historical",
        "adaptive",
        "manual_link",
        "recovery",
        "target_scan",
    }
)
_CURSOR_KINDS = frozenset(
    {"hot", "historical", "adaptive", "source_guard", "auto_sync", "target_index"}
)
_WORKER_TYPES = frozenset(
    {
        "download",
        "upload",
        "link",
        "button",
        "retry",
        "album",
        "cleanup",
        "db_writer",
        "db_critical",
        "db_job_state",
        "scanner",
        "reconciler",
        "queue_healer",
        "gateway",
    }
)
_WORKER_STATUSES = frozenset(
    {"starting", "idle", "working", "waiting", "stopping", "stopped", "failed"}
)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _mapping(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    raise DatabaseError("PostgreSQL repositories require mapping rows")


def _rowcount(result: Any) -> int:
    return max(0, int(getattr(result, "rowcount", 0) or 0))


def _required_text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise DatabaseError(f"{field} is required")
    return normalized


def _chat_id(value: Any, field: str = "source_chat_id") -> int:
    try:
        normalized = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise DatabaseError(f"{field} must be an integer") from exc
    if normalized == 0:
        raise DatabaseError(f"{field} cannot be zero")
    return normalized


def _priority_class(priority: int) -> str:
    if priority >= 900:
        return "critical"
    if priority >= 600:
        return "interactive"
    if priority >= 300:
        return "realtime"
    if priority >= 100:
        return "hot"
    if priority >= 0:
        return "historical"
    return "maintenance"


def _priority_for_class(priority: int, priority_class: str) -> int:
    ranges = {
        "critical": (900, 1000),
        "interactive": (600, 899),
        "realtime": (300, 599),
        "hot": (100, 299),
        "historical": (0, 99),
        "maintenance": (-1000, -1),
    }
    low, high = ranges[priority_class]
    return max(low, min(high, int(priority)))


class _Repository:
    def __init__(self, database: DatabaseInterface) -> None:
        self.database = database


class PostedRepository(_Repository, PostedRepositoryInterface):
    """Legacy posted hashes represented by canonical deduplication keys."""

    def exists(self, media_hash: str) -> bool:
        return self.database.fetchone(
            """
            SELECT 1
            FROM royells.deduplication_keys
            WHERE dedup_key=? AND key_kind='legacy_hash' AND status='active'
            LIMIT 1
            """,
            (_required_text(media_hash, "media_hash"),),
        ) is not None

    def add(self, media_hash: str, channel: str = "") -> None:
        key = _required_text(media_hash, "media_hash")
        self.database.execute(
            """
            INSERT INTO royells.deduplication_keys(
                dedup_key, key_kind, status, released_at, metadata
            )
            VALUES (?, 'legacy_hash', 'active', NULL, ?::jsonb)
            ON CONFLICT(dedup_key) DO UPDATE SET
                key_kind='legacy_hash',
                status='active',
                released_at=NULL,
                metadata=EXCLUDED.metadata,
                updated_at=clock_timestamp()
            """,
            (key, _json({"channel": str(channel or "")})),
        )

    def remove(self, media_hash: str) -> bool:
        result = self.database.execute(
            """
            UPDATE royells.deduplication_keys
            SET status='released',
                released_at=clock_timestamp(),
                updated_at=clock_timestamp()
            WHERE dedup_key=? AND key_kind='legacy_hash' AND status='active'
            """,
            (_required_text(media_hash, "media_hash"),),
        )
        return _rowcount(result) > 0


class TargetMediaRepository(_Repository, TargetMediaRepositoryInterface):
    """Target UID authority with optional target-chat materialization."""

    def __init__(
        self,
        database: DatabaseInterface,
        *,
        target_chat_id: Optional[int] = None,
    ) -> None:
        super().__init__(database)
        self.target_chat_id = (
            None if target_chat_id is None else _chat_id(target_chat_id, "target_chat_id")
        )

    def exists(self, uid: str) -> bool:
        return self.database.fetchone(
            """
            SELECT 1
            FROM royells.deduplication_keys
            WHERE dedup_key=? AND key_kind='media_uid' AND status='active'
            LIMIT 1
            """,
            (_required_text(uid, "uid"),),
        ) is not None

    def upsert(
        self,
        uid: str,
        *,
        message_id: int = 0,
        indexed_at: str = "",
        source: str = "target_scan",
    ) -> None:
        media_uid = _required_text(uid, "uid")
        with self.database.transaction():
            self.database.execute(
                """
                INSERT INTO royells.media_objects(media_uid, media_type, metadata)
                VALUES (?, 'unknown', '{}'::jsonb)
                ON CONFLICT(media_uid) DO UPDATE SET
                    last_seen_at=clock_timestamp()
                """,
                (media_uid,),
            )
            self.database.execute(
                """
                INSERT INTO royells.deduplication_keys(
                    dedup_key, key_kind, media_uid, status, released_at, metadata
                )
                VALUES (?, 'media_uid', ?, 'active', NULL, ?::jsonb)
                ON CONFLICT(dedup_key) DO UPDATE SET
                    key_kind='media_uid',
                    media_uid=EXCLUDED.media_uid,
                    status='active',
                    released_at=NULL,
                    metadata=EXCLUDED.metadata,
                    updated_at=clock_timestamp()
                """,
                (
                    media_uid,
                    media_uid,
                    _json({"indexed_at": indexed_at, "source": source}),
                ),
            )
            if self.target_chat_id is not None and int(message_id or 0) > 0:
                self.database.execute(
                    """
                    INSERT INTO royells.target_media_registry(
                        media_uid, target_chat_id, target_message_id,
                        registry_status, source, indexed_at, deleted_at, metadata
                    )
                    VALUES (
                        ?, ?, ?, 'active', ?,
                        COALESCE(NULLIF(?, '')::timestamptz, clock_timestamp()),
                        NULL, '{}'::jsonb
                    )
                    ON CONFLICT(media_uid) DO UPDATE SET
                        target_chat_id=EXCLUDED.target_chat_id,
                        target_message_id=EXCLUDED.target_message_id,
                        registry_status='active',
                        source=EXCLUDED.source,
                        indexed_at=EXCLUDED.indexed_at,
                        updated_at=clock_timestamp(),
                        deleted_at=NULL
                    """,
                    (
                        media_uid,
                        self.target_chat_id,
                        int(message_id),
                        _required_text(source, "source"),
                        str(indexed_at or ""),
                    ),
                )

    def remove(self, uid: str) -> bool:
        media_uid = _required_text(uid, "uid")
        with self.database.transaction():
            registry_result = self.database.execute(
                """
                UPDATE royells.target_media_registry
                SET registry_status='deleted',
                    deleted_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE media_uid=? AND registry_status<>'deleted'
                """,
                (media_uid,),
            )
            dedupe_result = self.database.execute(
                """
                UPDATE royells.deduplication_keys
                SET status='tombstoned',
                    released_at=clock_timestamp(),
                    updated_at=clock_timestamp()
                WHERE dedup_key=? AND key_kind='media_uid' AND status='active'
                """,
                (media_uid,),
            )
        return _rowcount(registry_result) > 0 or _rowcount(dedupe_result) > 0


TargetRepository = TargetMediaRepository


class ChannelRepository(_Repository, ChannelRepositoryInterface):
    def get(self, channel_id: str) -> Optional[Mapping[str, Any]]:
        row = self.database.fetchone(
            """
            SELECT source_chat_id::text AS channel_id, title, source_link,
                   COALESCE(username, '') AS username, status, enabled,
                   priority, added_at, updated_at, last_error, metadata
            FROM royells.source_channels
            WHERE source_chat_id=?
            """,
            (_chat_id(channel_id),),
        )
        return _mapping(row)

    def list_all(self) -> list[Mapping[str, Any]]:
        return [
            _mapping(row) or {}
            for row in self.database.fetchall(
                """
                SELECT source_chat_id::text AS channel_id, title, source_link,
                       COALESCE(username, '') AS username, status, enabled,
                       priority, added_at, updated_at, last_error, metadata
                FROM royells.source_channels
                ORDER BY priority DESC, source_chat_id
                """
            )
        ]

    def upsert(self, channel: Mapping[str, Any]) -> None:
        source_chat_id = _chat_id(channel.get("channel_id"))
        status = str(channel.get("status") or "active").strip().lower()
        allowed_statuses = {
            "active",
            "paused",
            "unreachable",
            "left",
            "removed",
            "ignored_system",
        }
        if status not in allowed_statuses:
            status = "active"
        enabled = bool(channel.get("enabled", status == "active"))
        username = str(channel.get("username") or "").strip().lstrip("@") or None
        self.database.execute(
            """
            INSERT INTO royells.source_channels(
                source_chat_id, title, username, source_link, status, enabled,
                priority, added_at, updated_at, last_error, metadata
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                COALESCE(NULLIF(?, '')::timestamptz, clock_timestamp()),
                clock_timestamp(), ?, ?::jsonb
            )
            ON CONFLICT(source_chat_id) DO UPDATE SET
                title=EXCLUDED.title,
                username=EXCLUDED.username,
                source_link=EXCLUDED.source_link,
                status=EXCLUDED.status,
                enabled=EXCLUDED.enabled,
                priority=EXCLUDED.priority,
                updated_at=clock_timestamp(),
                last_error=EXCLUDED.last_error,
                metadata=EXCLUDED.metadata
            """,
            (
                source_chat_id,
                str(channel.get("title") or ""),
                username,
                str(channel.get("source_link") or ""),
                status,
                enabled,
                max(-1000, min(1000, int(channel.get("priority") or 0))),
                str(channel.get("added_at") or ""),
                str(channel.get("last_error") or ""),
                _json(dict(channel.get("metadata") or {})),
            ),
        )

    def remove(self, channel_id: str) -> bool:
        result = self.database.execute(
            """
            UPDATE royells.source_channels
            SET status='removed', enabled=false, updated_at=clock_timestamp()
            WHERE source_chat_id=? AND (status<>'removed' OR enabled)
            """,
            (_chat_id(channel_id),),
        )
        return _rowcount(result) > 0


class SubscriptionRepository(_Repository, SubscriptionRepositoryInterface):
    def get(self, user_id: str) -> Optional[Mapping[str, Any]]:
        row = self.database.fetchone(
            """
            SELECT user_id::text AS user_id, expire_at AS expire_date, status,
                   first_name, last_name, COALESCE(username, '') AS username,
                   profile_updated_at, created_at, updated_at, metadata
            FROM royells.subscriptions
            WHERE user_id=?
            """,
            (int(_required_text(user_id, "user_id")),),
        )
        return _mapping(row)

    def upsert(self, subscription: Mapping[str, Any]) -> None:
        user_id = int(_required_text(subscription.get("user_id"), "user_id"))
        if user_id <= 0:
            raise DatabaseError("user_id must be positive")
        status = str(subscription.get("status") or "active").lower()
        if status not in {"active", "expired", "banned", "removed"}:
            status = "active"
        username = (
            str(subscription.get("username") or "").strip().lstrip("@") or None
        )
        self.database.execute(
            """
            INSERT INTO royells.subscriptions(
                user_id, status, expire_at, first_name, last_name, username,
                profile_updated_at, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb)
            ON CONFLICT(user_id) DO UPDATE SET
                status=EXCLUDED.status,
                expire_at=EXCLUDED.expire_at,
                first_name=EXCLUDED.first_name,
                last_name=EXCLUDED.last_name,
                username=EXCLUDED.username,
                profile_updated_at=EXCLUDED.profile_updated_at,
                updated_at=clock_timestamp(),
                metadata=EXCLUDED.metadata
            """,
            (
                user_id,
                status,
                subscription.get("expire_date", subscription.get("expire_at")),
                str(subscription.get("first_name") or ""),
                str(subscription.get("last_name") or ""),
                username,
                subscription.get("profile_updated_at"),
                _json(dict(subscription.get("metadata") or {})),
            ),
        )

    def remove(self, user_id: str) -> bool:
        result = self.database.execute(
            """
            UPDATE royells.subscriptions
            SET status='removed', updated_at=clock_timestamp()
            WHERE user_id=? AND status<>'removed'
            """,
            (int(_required_text(user_id, "user_id")),),
        )
        return _rowcount(result) > 0


class DeadMediaRepository(_Repository, DeadMediaRepositoryInterface):
    def get(self, uid: str) -> Optional[Mapping[str, Any]]:
        row = self.database.fetchone(
            """
            SELECT media_uid AS uid, status, failure_count, reason_code,
                   reason, source_chat_id, source_title,
                   first_failed_at AS created_at,
                   last_failed_at AS updated_at, cleared_at, metadata
            FROM royells.dead_media
            WHERE media_uid=?
            """,
            (_required_text(uid, "uid"),),
        )
        return _mapping(row)

    def upsert(self, record: Mapping[str, Any]) -> None:
        media_uid = _required_text(
            record.get("uid", record.get("media_uid")),
            "uid",
        )
        source_chat_id = record.get("source_chat_id")
        normalized_source = (
            None
            if source_chat_id in (None, "")
            else _chat_id(source_chat_id)
        )
        with self.database.transaction():
            self.database.execute(
                """
                INSERT INTO royells.media_objects(media_uid, media_type, metadata)
                VALUES (?, 'unknown', '{}'::jsonb)
                ON CONFLICT(media_uid) DO UPDATE SET
                    last_seen_at=clock_timestamp()
                """,
                (media_uid,),
            )
            if normalized_source is not None:
                self.database.execute(
                    """
                    INSERT INTO royells.source_channels(
                        source_chat_id, title, status, enabled
                    )
                    VALUES (?, ?, 'unreachable', false)
                    ON CONFLICT(source_chat_id) DO NOTHING
                    """,
                    (
                        normalized_source,
                        str(record.get("source_title") or ""),
                    ),
                )
            status = str(record.get("status") or "dead").lower()
            if status not in {"retryable", "dead", "cleared"}:
                status = "dead"
            self.database.execute(
                """
                INSERT INTO royells.dead_media(
                    media_uid, status, failure_count, reason_code, reason,
                    source_chat_id, source_title, first_failed_at,
                    last_failed_at, cleared_at, metadata
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    COALESCE(NULLIF(?, '')::timestamptz, clock_timestamp()),
                    COALESCE(NULLIF(?, '')::timestamptz, clock_timestamp()),
                    CASE WHEN ?='cleared' THEN clock_timestamp() ELSE NULL END,
                    ?::jsonb
                )
                ON CONFLICT(media_uid) DO UPDATE SET
                    status=EXCLUDED.status,
                    failure_count=GREATEST(
                        royells.dead_media.failure_count,
                        EXCLUDED.failure_count
                    ),
                    reason_code=EXCLUDED.reason_code,
                    reason=EXCLUDED.reason,
                    source_chat_id=EXCLUDED.source_chat_id,
                    source_title=EXCLUDED.source_title,
                    last_failed_at=EXCLUDED.last_failed_at,
                    cleared_at=EXCLUDED.cleared_at,
                    metadata=EXCLUDED.metadata
                """,
                (
                    media_uid,
                    status,
                    max(1, int(record.get("failure_count") or 1)),
                    str(record.get("reason_code") or ""),
                    str(record.get("reason") or ""),
                    normalized_source,
                    str(record.get("source_title") or ""),
                    str(record.get("created_at") or ""),
                    str(record.get("updated_at") or ""),
                    status,
                    _json(dict(record.get("metadata") or {})),
                ),
            )

    def remove(self, uid: str) -> bool:
        result = self.database.execute(
            """
            UPDATE royells.dead_media
            SET status='cleared', cleared_at=clock_timestamp(),
                last_failed_at=clock_timestamp()
            WHERE media_uid=? AND status<>'cleared'
            """,
            (_required_text(uid, "uid"),),
        )
        return _rowcount(result) > 0


class MetricsRepository(_Repository, MetricsRepositoryInterface):
    def record(
        self,
        name: str,
        value: float,
        *,
        recorded_at: str,
        labels: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.database.execute(
            """
            INSERT INTO royells.metrics_samples(
                metric_name, metric_value, labels, observed_at
            )
            VALUES (
                ?, ?, ?::jsonb,
                COALESCE(NULLIF(?, '')::timestamptz, clock_timestamp())
            )
            """,
            (
                _required_text(name, "metric name"),
                float(value),
                _json(dict(labels or {})),
                str(recorded_at or ""),
            ),
        )

    def latest(self, name: str, limit: int = 100) -> list[Mapping[str, Any]]:
        return [
            _mapping(row) or {}
            for row in self.database.fetchall(
                """
                SELECT metric_name AS name, metric_value AS value,
                       labels, observed_at AS recorded_at
                FROM royells.metrics_samples
                WHERE metric_name=?
                ORDER BY observed_at DESC, metric_sample_id DESC
                LIMIT ?
                """,
                (
                    _required_text(name, "metric name"),
                    max(1, min(10000, int(limit))),
                ),
            )
        ]


class JobRepository(_Repository, JobRepositoryInterface):
    def get(self, job_id: str) -> Optional[Mapping[str, Any]]:
        row = self.database.fetchone(
            """
            SELECT job_id, post_uid, job_type, source_kind AS source,
                   source_chat_id, source_name AS ch_name, status,
                   current_stage AS stage, priority,
                   attempt_count AS attempt, max_attempts, not_before,
                   last_error_code, last_error, created_at, updated_at,
                   completed_at, row_version, metadata
            FROM royells.media_jobs
            WHERE job_id=?
            """,
            (_required_text(job_id, "job_id"),),
        )
        return _mapping(row)

    def upsert(self, job: Mapping[str, Any]) -> None:
        job_id = _required_text(job.get("job_id"), "job_id")
        job_type = str(job.get("job_type", job.get("type", "single"))).lower()
        if job_type not in _JOB_TYPES:
            job_type = "single"
        source_kind = str(job.get("source_kind", job.get("source", "source"))).lower()
        if source_kind not in _SOURCE_KINDS:
            source_kind = "source"
        status = str(job.get("status") or "queued").lower()
        if status not in _JOB_STATUSES:
            status = "queued"
        source_chat_raw = job.get("source_chat_id", job.get("channel_id"))
        source_chat_id = (
            None
            if source_chat_raw in (None, "")
            else _chat_id(source_chat_raw)
        )
        source_name = str(job.get("source_name", job.get("ch_name", "")) or "")
        with self.database.transaction():
            if source_chat_id is not None:
                self.database.execute(
                    """
                    INSERT INTO royells.source_channels(
                        source_chat_id, title, status, enabled
                    )
                    VALUES (?, ?, 'active', true)
                    ON CONFLICT(source_chat_id) DO NOTHING
                    """,
                    (source_chat_id, source_name),
                )
            self.database.execute(
                """
                INSERT INTO royells.media_jobs(
                    job_id, post_uid, job_type, source_kind, source_chat_id,
                    source_name, status, current_stage, priority,
                    attempt_count, max_attempts, not_before,
                    last_error_code, last_error, completed_at, metadata
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE(?, clock_timestamp()), ?, ?,
                    CASE WHEN ? THEN clock_timestamp() ELSE NULL END,
                    ?::jsonb
                )
                ON CONFLICT(job_id) DO UPDATE SET
                    post_uid=EXCLUDED.post_uid,
                    job_type=EXCLUDED.job_type,
                    source_kind=EXCLUDED.source_kind,
                    source_chat_id=EXCLUDED.source_chat_id,
                    source_name=EXCLUDED.source_name,
                    status=EXCLUDED.status,
                    current_stage=EXCLUDED.current_stage,
                    priority=EXCLUDED.priority,
                    attempt_count=EXCLUDED.attempt_count,
                    max_attempts=EXCLUDED.max_attempts,
                    not_before=EXCLUDED.not_before,
                    last_error_code=EXCLUDED.last_error_code,
                    last_error=EXCLUDED.last_error,
                    completed_at=EXCLUDED.completed_at,
                    metadata=EXCLUDED.metadata,
                    row_version=royells.media_jobs.row_version + 1,
                    updated_at=clock_timestamp()
                """,
                (
                    job_id,
                    str(job.get("post_uid") or ""),
                    job_type,
                    source_kind,
                    source_chat_id,
                    source_name,
                    status,
                    str(job.get("stage", job.get("current_stage", "")) or ""),
                    max(-1000, min(1000, int(job.get("priority") or 0))),
                    max(0, int(job.get("attempt", job.get("attempt_count", 0)) or 0)),
                    max(1, int(job.get("max_attempts") or 12)),
                    job.get("not_before"),
                    str(job.get("last_error_code") or ""),
                    str(job.get("last_error") or "")[:4000],
                    status in _TERMINAL_JOB_STATUSES,
                    _json(dict(job)),
                ),
            )

    def list_unfinished(self, limit: int = 1000) -> list[Mapping[str, Any]]:
        return [
            _mapping(row) or {}
            for row in self.database.fetchall(
                """
                SELECT job_id, post_uid, job_type, source_kind AS source,
                       source_chat_id, source_name AS ch_name, status,
                       current_stage AS stage, priority,
                       attempt_count AS attempt, max_attempts, not_before,
                       last_error, created_at, updated_at, row_version, metadata
                FROM royells.media_jobs
                WHERE status NOT IN ('completed', 'failed', 'dead', 'cancelled')
                ORDER BY priority DESC, not_before, created_at
                LIMIT ?
                """,
                (max(1, min(100000, int(limit))),),
            )
        ]

    def transition(
        self,
        job_id: str,
        *,
        stage: str,
        status: str,
        error: str = "",
    ) -> bool:
        normalized_status = str(status).lower()
        if normalized_status not in _JOB_STATUSES:
            raise DatabaseError(f"Unsupported PostgreSQL job status: {status}")
        result = self.database.execute(
            """
            UPDATE royells.media_jobs
            SET current_stage=?,
                status=?,
                last_error=?,
                completed_at=CASE
                    WHEN ? THEN COALESCE(completed_at, clock_timestamp())
                    ELSE NULL
                END,
                row_version=row_version + 1,
                updated_at=clock_timestamp()
            WHERE job_id=?
            """,
            (
                str(stage or ""),
                normalized_status,
                str(error or "")[:4000],
                normalized_status in _TERMINAL_JOB_STATUSES,
                _required_text(job_id, "job_id"),
            ),
        )
        return _rowcount(result) > 0


class QueueStateRepository(_Repository, QueueStateRepositoryInterface):
    def _name(self, queue_name: str) -> str:
        normalized = str(queue_name).strip().lower()
        if normalized not in _QUEUE_NAMES:
            raise DatabaseError(f"Unsupported PostgreSQL queue: {queue_name}")
        return normalized

    def load(self, queue_name: str) -> Mapping[str, Any]:
        name = self._name(queue_name)
        runtime = _mapping(
            self.database.fetchone(
                """
                SELECT queue_name, global_virtual_time, lane_cursor,
                       lane_epoch, row_version, updated_at
                FROM royells.queue_runtime_state
                WHERE queue_name=?
                """,
                (name,),
            )
        ) or {
            "queue_name": name,
            "global_virtual_time": 0,
            "lane_cursor": 0,
            "lane_epoch": 0,
            "row_version": 0,
        }
        runtime["items"] = list(self.list_pending(name))
        return runtime

    def save(self, queue_name: str, state: Mapping[str, Any]) -> None:
        name = self._name(queue_name)
        self.database.execute(
            """
            INSERT INTO royells.queue_runtime_state(
                queue_name, global_virtual_time, lane_cursor, lane_epoch
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(queue_name) DO UPDATE SET
                global_virtual_time=EXCLUDED.global_virtual_time,
                lane_cursor=EXCLUDED.lane_cursor,
                lane_epoch=EXCLUDED.lane_epoch,
                row_version=royells.queue_runtime_state.row_version + 1,
                updated_at=clock_timestamp()
            """,
            (
                name,
                max(0.0, float(state.get("global_virtual_time") or 0.0)),
                max(0, int(state.get("lane_cursor") or 0)),
                max(0, int(state.get("lane_epoch") or 0)),
            ),
        )

    def list_pending(self, queue_name: str) -> Sequence[Mapping[str, Any]]:
        name = self._name(queue_name)
        return tuple(
            _mapping(row) or {}
            for row in self.database.fetchall(
                """
                SELECT queue_entry_id, job_id, queue_name, next_queue_name,
                       state, priority, priority_class, fairness_key,
                       estimated_cost, virtual_start, virtual_finish,
                       queue_order, available_at, lease_owner_worker_id,
                       lease_token, lease_expires_at, lease_heartbeat_at,
                       lease_failures, attempt_count, last_error,
                       enqueued_at, updated_at, row_version, metadata
                FROM royells.job_queue_entries
                WHERE queue_name=? AND state IN ('ready', 'delayed', 'leased')
                ORDER BY priority DESC, virtual_finish, queue_order
                """,
                (name,),
            )
        )


class CursorRepository(_Repository, CursorRepositoryInterface):
    def get(
        self,
        source_id: str,
        cursor_kind: str,
    ) -> Optional[Mapping[str, Any]]:
        kind = str(cursor_kind).strip().lower()
        if kind not in _CURSOR_KINDS:
            raise DatabaseError(f"Unsupported cursor kind: {cursor_kind}")
        return _mapping(
            self.database.fetchone(
                """
                SELECT source_chat_id::text AS source_id, cursor_kind,
                       next_message_id, last_scanned_message_id,
                       high_watermark_message_id, completed, cursor_version,
                       cursor_payload, last_success_at, last_attempt_at,
                       last_error, updated_at
                FROM royells.source_scan_cursors
                WHERE source_chat_id=? AND cursor_kind=?
                """,
                (_chat_id(source_id), kind),
            )
        )

    def upsert(
        self,
        source_id: str,
        cursor_kind: str,
        cursor: Mapping[str, Any],
    ) -> None:
        source_chat_id = _chat_id(source_id)
        kind = str(cursor_kind).strip().lower()
        if kind not in _CURSOR_KINDS:
            raise DatabaseError(f"Unsupported cursor kind: {cursor_kind}")
        with self.database.transaction():
            self.database.execute(
                """
                INSERT INTO royells.source_channels(
                    source_chat_id, status, enabled
                )
                VALUES (?, 'active', true)
                ON CONFLICT(source_chat_id) DO NOTHING
                """,
                (source_chat_id,),
            )
            self.database.execute(
                """
                INSERT INTO royells.source_scan_cursors(
                    source_chat_id, cursor_kind, next_message_id,
                    last_scanned_message_id, high_watermark_message_id,
                    completed, cursor_version, cursor_payload,
                    last_success_at, last_attempt_at, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?)
                ON CONFLICT(source_chat_id, cursor_kind) DO UPDATE SET
                    next_message_id=EXCLUDED.next_message_id,
                    last_scanned_message_id=EXCLUDED.last_scanned_message_id,
                    high_watermark_message_id=EXCLUDED.high_watermark_message_id,
                    completed=EXCLUDED.completed,
                    cursor_version=GREATEST(
                        royells.source_scan_cursors.cursor_version + 1,
                        EXCLUDED.cursor_version
                    ),
                    cursor_payload=EXCLUDED.cursor_payload,
                    last_success_at=EXCLUDED.last_success_at,
                    last_attempt_at=EXCLUDED.last_attempt_at,
                    last_error=EXCLUDED.last_error,
                    updated_at=clock_timestamp()
                """,
                (
                    source_chat_id,
                    kind,
                    cursor.get("next_message_id"),
                    cursor.get("last_scanned_message_id"),
                    cursor.get("high_watermark_message_id"),
                    bool(cursor.get("completed", False)),
                    max(1, int(cursor.get("cursor_version") or 1)),
                    _json(dict(cursor.get("cursor_payload") or cursor)),
                    cursor.get("last_success_at"),
                    cursor.get("last_attempt_at"),
                    str(cursor.get("last_error") or "")[:4000],
                ),
            )

    def list_incomplete(self, cursor_kind: str) -> Sequence[Mapping[str, Any]]:
        kind = str(cursor_kind).strip().lower()
        if kind not in _CURSOR_KINDS:
            raise DatabaseError(f"Unsupported cursor kind: {cursor_kind}")
        return tuple(
            _mapping(row) or {}
            for row in self.database.fetchall(
                """
                SELECT source_chat_id::text AS source_id, cursor_kind,
                       next_message_id, last_scanned_message_id,
                       high_watermark_message_id, completed, cursor_version,
                       cursor_payload, last_success_at, last_attempt_at,
                       last_error, updated_at
                FROM royells.source_scan_cursors
                WHERE cursor_kind=? AND completed=false
                ORDER BY updated_at, source_chat_id
                """,
                (kind,),
            )
        )


class WorkerRepository(_Repository, WorkerRepositoryInterface):
    def __init__(
        self,
        database: DatabaseInterface,
        *,
        instance_id: str = "book18-adapter",
    ) -> None:
        super().__init__(database)
        self.instance_id = _required_text(instance_id, "instance_id")

    def _ensure_instance(self) -> None:
        self.database.execute(
            """
            INSERT INTO royells.bot_instances(
                instance_id, boot_id, app_version, status, metadata
            )
            VALUES (?, ?, '20.0.0', 'running', '{"adapter":true}'::jsonb)
            ON CONFLICT(instance_id) DO UPDATE SET
                heartbeat_at=clock_timestamp(),
                status=CASE
                    WHEN royells.bot_instances.status='superseded'
                    THEN royells.bot_instances.status
                    ELSE 'running'
                END
            """,
            (self.instance_id, self.instance_id),
        )

    def heartbeat(self, worker: Mapping[str, Any]) -> None:
        worker_id = _required_text(worker.get("worker_id"), "worker_id")
        worker_type = str(worker.get("worker_type") or "scanner").lower()
        if worker_type not in _WORKER_TYPES:
            worker_type = "scanner"
        status = str(worker.get("status") or "working").lower()
        if status not in _WORKER_STATUSES:
            status = "working"
        with self.database.transaction():
            self._ensure_instance()
            self.database.execute(
                """
                INSERT INTO royells.worker_instances(
                    worker_id, instance_id, worker_type, ordinal, status,
                    heartbeat_at, stopped_at, last_error, metadata
                )
                VALUES (
                    ?, ?, ?, ?, ?, clock_timestamp(),
                    CASE WHEN ? IN ('stopped', 'failed')
                         THEN clock_timestamp() ELSE NULL END,
                    ?, ?::jsonb
                )
                ON CONFLICT(worker_id) DO UPDATE SET
                    instance_id=EXCLUDED.instance_id,
                    worker_type=EXCLUDED.worker_type,
                    ordinal=EXCLUDED.ordinal,
                    status=EXCLUDED.status,
                    heartbeat_at=clock_timestamp(),
                    stopped_at=EXCLUDED.stopped_at,
                    last_error=EXCLUDED.last_error,
                    metadata=EXCLUDED.metadata
                """,
                (
                    worker_id,
                    self.instance_id,
                    worker_type,
                    max(0, int(worker.get("ordinal") or 0)),
                    status,
                    status,
                    str(worker.get("last_error") or "")[:4000],
                    _json(dict(worker.get("metadata") or worker)),
                ),
            )

    def remove(self, worker_id: str) -> bool:
        result = self.database.execute(
            """
            UPDATE royells.worker_instances
            SET status='stopped', stopped_at=clock_timestamp(),
                heartbeat_at=clock_timestamp()
            WHERE worker_id=? AND status<>'stopped'
            """,
            (_required_text(worker_id, "worker_id"),),
        )
        return _rowcount(result) > 0

    def list_live(self, max_age_seconds: float = 120.0) -> Sequence[Mapping[str, Any]]:
        return tuple(
            _mapping(row) or {}
            for row in self.database.fetchall(
                """
                SELECT worker_id, instance_id, worker_type, ordinal, status,
                       started_at, heartbeat_at, stopped_at, last_error, metadata
                FROM royells.worker_instances
                WHERE status NOT IN ('stopped', 'failed')
                  AND heartbeat_at >= (
                      clock_timestamp() - (? * interval '1 second')
                  )
                ORDER BY worker_type, ordinal, worker_id
                """,
                (max(1.0, float(max_age_seconds)),),
            )
        )


class CheckpointRepository(_Repository, CheckpointRepositoryInterface):
    def latest(self, name: str = "runtime_checkpoint") -> Optional[Mapping[str, Any]]:
        return _mapping(
            self.database.fetchone(
                """
                SELECT checkpoint_id, checkpoint_kind AS name, schema_version,
                       app_version, sequence_no AS revision,
                       checksum_sha256 AS sha256, checkpoint_status,
                       payload, instance_id, created_at, rejection_reason
                FROM royells.runtime_checkpoints
                WHERE checkpoint_kind=? AND checkpoint_status='current'
                ORDER BY sequence_no DESC
                LIMIT 1
                """,
                (_required_text(name, "checkpoint name"),),
            )
        )

    def save(self, name: str, checkpoint: Mapping[str, Any]) -> None:
        checkpoint_name = _required_text(name, "checkpoint name")
        payload = checkpoint.get("payload", checkpoint)
        encoded = _json(payload)
        checksum = str(checkpoint.get("sha256") or "").lower()
        if len(checksum) != 64:
            checksum = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        revision = max(
            1,
            int(
                checkpoint.get(
                    "revision",
                    checkpoint.get("sequence_no", int(time.time() * 1000)),
                )
            ),
        )
        with self.database.transaction():
            self.database.execute(
                """
                UPDATE royells.runtime_checkpoints
                SET checkpoint_status='superseded'
                WHERE checkpoint_kind=? AND checkpoint_status='current'
                """,
                (checkpoint_name,),
            )
            self.database.execute(
                """
                INSERT INTO royells.runtime_checkpoints(
                    checkpoint_kind, schema_version, app_version, sequence_no,
                    checksum_sha256, checkpoint_status, payload, instance_id
                )
                VALUES (?, ?, ?, ?, ?, 'current', ?::jsonb, ?)
                ON CONFLICT(checkpoint_kind, sequence_no) DO UPDATE SET
                    schema_version=EXCLUDED.schema_version,
                    app_version=EXCLUDED.app_version,
                    checksum_sha256=EXCLUDED.checksum_sha256,
                    checkpoint_status='current',
                    payload=EXCLUDED.payload,
                    instance_id=EXCLUDED.instance_id,
                    rejection_reason=''
                """,
                (
                    checkpoint_name,
                    max(1, int(checkpoint.get("schema_version") or 1)),
                    _required_text(
                        checkpoint.get("app_version", "20.0.0"),
                        "app_version",
                    ),
                    revision,
                    checksum,
                    encoded,
                    checkpoint.get("instance_id"),
                ),
            )


class AuditRepository(_Repository, AuditRepositoryInterface):
    def append(
        self,
        action: str,
        *,
        object_type: str,
        object_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        event_id = f"audit_{uuid.uuid4().hex}"
        details = dict(metadata or {})
        details.setdefault("event_id", event_id)
        self.database.execute(
            """
            INSERT INTO royells.audit_events(
                actor_type, actor_id, action, object_type, object_id,
                result, details
            )
            VALUES ('system', '', ?, ?, ?, 'success', ?::jsonb)
            """,
            (
                _required_text(action, "action"),
                str(object_type or ""),
                str(object_id or ""),
                _json(details),
            ),
        )
        return event_id

    def latest(self, limit: int = 100) -> Sequence[Mapping[str, Any]]:
        return tuple(
            _mapping(row) or {}
            for row in self.database.fetchall(
                """
                SELECT event_at, audit_event_id, actor_type, actor_id, action,
                       object_type, object_id, result, details
                FROM royells.audit_events
                ORDER BY event_at DESC, audit_event_id DESC
                LIMIT ?
                """,
                (max(1, min(10000, int(limit))),),
            )
        )


class RuntimeRepository(_Repository):
    """Compatibility access to current PostgreSQL runtime checkpoints."""

    def load(self, state_key: str) -> Optional[Mapping[str, Any]]:
        return CheckpointRepository(self.database).latest(state_key)

    def save(
        self,
        state_key: str,
        payload: Any,
        *,
        schema_version: int,
        revision: int,
    ) -> None:
        CheckpointRepository(self.database).save(
            state_key,
            {
                "payload": payload,
                "schema_version": schema_version,
                "revision": revision,
                "app_version": "20.0.0",
            },
        )
