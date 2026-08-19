"""Database-neutral repositories matching the current Royells schema."""

from __future__ import annotations

import json
import time
import uuid
from copy import deepcopy
from typing import Any, Mapping, Optional

from ..errors import DatabaseError
from ..interfaces import DatabaseInterface
from ..interfaces import RuntimeStateInterface
from .contracts import (
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


def _mapping(row: Any, columns: tuple[str, ...]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return {column: row[column] for column in columns}
    return dict(zip(columns, row))


class _Repository:
    def __init__(self, database: DatabaseInterface) -> None:
        self.database = database


class PostedRepository(_Repository, PostedRepositoryInterface):
    def exists(self, media_hash: str) -> bool:
        return (
            self.database.fetchone(
                "SELECT 1 FROM posted WHERE hash=? LIMIT 1",
                (str(media_hash),),
            )
            is not None
        )

    def add(self, media_hash: str, channel: str = "") -> None:
        with self.database.transaction():
            if self.exists(media_hash):
                self.database.update(
                    "posted",
                    {"channel": str(channel)},
                    {"hash": str(media_hash)},
                )
            else:
                try:
                    self.database.insert(
                        "posted",
                        {"hash": str(media_hash), "channel": str(channel)},
                    )
                except DatabaseError:
                    if not self.exists(media_hash):
                        raise

    def remove(self, media_hash: str) -> bool:
        with self.database.transaction():
            return self.database.delete("posted", {"hash": str(media_hash)}) > 0


class TargetMediaRepository(_Repository, TargetMediaRepositoryInterface):
    def exists(self, uid: str) -> bool:
        return (
            self.database.fetchone(
                "SELECT 1 FROM target_media WHERE uid=? LIMIT 1",
                (str(uid),),
            )
            is not None
        )

    def upsert(
        self,
        uid: str,
        *,
        message_id: int = 0,
        indexed_at: str = "",
        source: str = "target_scan",
    ) -> None:
        uid = str(uid)
        with self.database.transaction():
            if not self.exists(uid):
                try:
                    self.database.insert("target_media", {"uid": uid})
                except DatabaseError:
                    if not self.exists(uid):
                        raise
            index_exists = (
                self.database.fetchone(
                    "SELECT 1 FROM target_media_full_index WHERE uid=? LIMIT 1",
                    (uid,),
                )
                is not None
            )
            values = {
                "message_id": int(message_id),
                "indexed_at": str(indexed_at),
                "source": str(source),
            }
            if index_exists:
                self.database.update(
                    "target_media_full_index",
                    values,
                    {"uid": uid},
                )
            else:
                try:
                    self.database.insert(
                        "target_media_full_index",
                        {"uid": uid, **values},
                    )
                except DatabaseError:
                    if (
                        self.database.fetchone(
                            "SELECT 1 FROM target_media_full_index WHERE uid=? LIMIT 1",
                            (uid,),
                        )
                        is None
                    ):
                        raise
                    self.database.update(
                        "target_media_full_index",
                        values,
                        {"uid": uid},
                    )

    def remove(self, uid: str) -> bool:
        uid = str(uid)
        with self.database.transaction():
            self.database.delete("target_media_full_index", {"uid": uid})
            return self.database.delete("target_media", {"uid": uid}) > 0


TargetRepository = TargetMediaRepository


class ChannelRepository(_Repository, ChannelRepositoryInterface):
    COLUMNS = (
        "channel_id",
        "title",
        "source_link",
        "username",
        "added_at",
        "updated_at",
    )

    def get(self, channel_id: str) -> Optional[Mapping[str, Any]]:
        row = self.database.fetchone(
            """
            SELECT channel_id, title, source_link, username, added_at, updated_at
            FROM channels WHERE channel_id=?
            """,
            (str(channel_id),),
        )
        return _mapping(row, self.COLUMNS)

    def list_all(self) -> list[Mapping[str, Any]]:
        rows = self.database.fetchall(
            """
            SELECT channel_id, title, source_link, username, added_at, updated_at
            FROM channels ORDER BY channel_id
            """
        )
        return [_mapping(row, self.COLUMNS) or {} for row in rows]

    def upsert(self, channel: Mapping[str, Any]) -> None:
        channel_id = str(channel.get("channel_id") or "").strip()
        if not channel_id:
            raise DatabaseError("Channel requires channel_id")
        values = {
            "title": str(channel.get("title") or ""),
            "source_link": str(channel.get("source_link") or ""),
            "username": str(channel.get("username") or "").lstrip("@"),
            "added_at": str(channel.get("added_at") or ""),
            "updated_at": str(channel.get("updated_at") or ""),
        }
        with self.database.transaction():
            if self.get(channel_id) is not None:
                self.database.update("channels", values, {"channel_id": channel_id})
            else:
                try:
                    self.database.insert(
                        "channels",
                        {"channel_id": channel_id, **values},
                    )
                except DatabaseError:
                    if self.get(channel_id) is None:
                        raise
                    self.database.update(
                        "channels", values, {"channel_id": channel_id}
                    )

    def remove(self, channel_id: str) -> bool:
        with self.database.transaction():
            return (
                self.database.delete(
                    "channels", {"channel_id": str(channel_id)}
                )
                > 0
            )


class SubscriptionRepository(_Repository, SubscriptionRepositoryInterface):
    COLUMNS = (
        "user_id",
        "expire_date",
        "first_name",
        "last_name",
        "username",
        "profile_updated_at",
    )

    def get(self, user_id: str) -> Optional[Mapping[str, Any]]:
        row = self.database.fetchone(
            """
            SELECT user_id, expire_date, first_name, last_name, username,
                   profile_updated_at
            FROM subscriptions WHERE user_id=?
            """,
            (str(user_id),),
        )
        return _mapping(row, self.COLUMNS)

    def upsert(self, subscription: Mapping[str, Any]) -> None:
        user_id = str(subscription.get("user_id") or "").strip()
        if not user_id:
            raise DatabaseError("Subscription requires user_id")
        values = {
            "expire_date": subscription.get("expire_date"),
            "first_name": str(subscription.get("first_name") or ""),
            "last_name": str(subscription.get("last_name") or ""),
            "username": str(subscription.get("username") or "").lstrip("@"),
            "profile_updated_at": str(
                subscription.get("profile_updated_at") or ""
            ),
        }
        with self.database.transaction():
            if self.get(user_id) is not None:
                self.database.update(
                    "subscriptions", values, {"user_id": user_id}
                )
            else:
                try:
                    self.database.insert(
                        "subscriptions",
                        {"user_id": user_id, **values},
                    )
                except DatabaseError:
                    if self.get(user_id) is None:
                        raise
                    self.database.update(
                        "subscriptions", values, {"user_id": user_id}
                    )

    def remove(self, user_id: str) -> bool:
        with self.database.transaction():
            return (
                self.database.delete(
                    "subscriptions", {"user_id": str(user_id)}
                )
                > 0
            )


class DeadMediaRepository(_Repository, DeadMediaRepositoryInterface):
    COLUMNS = (
        "uid",
        "failure_count",
        "reason",
        "source_chat_id",
        "source_title",
        "created_at",
        "updated_at",
    )

    def get(self, uid: str) -> Optional[Mapping[str, Any]]:
        row = self.database.fetchone(
            """
            SELECT uid, failure_count, reason, source_chat_id, source_title,
                   created_at, updated_at
            FROM dead_media WHERE uid=?
            """,
            (str(uid),),
        )
        return _mapping(row, self.COLUMNS)

    def upsert(self, record: Mapping[str, Any]) -> None:
        uid = str(record.get("uid") or "").strip()
        if not uid:
            raise DatabaseError("Dead-media record requires uid")
        values = {
            "failure_count": int(record.get("failure_count") or 0),
            "reason": str(record.get("reason") or ""),
            "source_chat_id": str(record.get("source_chat_id") or ""),
            "source_title": str(record.get("source_title") or ""),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
        }
        with self.database.transaction():
            if self.get(uid) is not None:
                self.database.update("dead_media", values, {"uid": uid})
            else:
                try:
                    self.database.insert("dead_media", {"uid": uid, **values})
                except DatabaseError:
                    if self.get(uid) is None:
                        raise
                    self.database.update("dead_media", values, {"uid": uid})

    def remove(self, uid: str) -> bool:
        with self.database.transaction():
            return self.database.delete("dead_media", {"uid": str(uid)}) > 0


class MetricsRepository(_Repository, MetricsRepositoryInterface):
    """Metrics repository; schema ownership belongs to database migrations."""

    def record(
        self,
        name: str,
        value: float,
        *,
        recorded_at: str,
        labels: Optional[Mapping[str, Any]] = None,
    ) -> None:
        with self.database.transaction():
            self.database.insert(
                "book17_metrics",
                {
                    "name": str(name),
                    "value": float(value),
                    "labels_json": json.dumps(
                        dict(labels or {}),
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                    "recorded_at": str(recorded_at),
                },
            )

    def latest(self, name: str, limit: int = 100) -> list[Mapping[str, Any]]:
        safe_limit = max(1, min(10000, int(limit)))
        rows = self.database.fetchall(
            """
            SELECT name, value, labels_json, recorded_at
            FROM book17_metrics
            WHERE name=?
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (str(name), safe_limit),
        )
        result: list[Mapping[str, Any]] = []
        for row in rows:
            item = _mapping(row, ("name", "value", "labels_json", "recorded_at"))
            assert item is not None
            try:
                labels = json.loads(item.pop("labels_json"))
            except (TypeError, json.JSONDecodeError):
                labels = {}
            item["labels"] = labels
            result.append(item)
        return result


class JobRepository(_Repository, JobRepositoryInterface):
    """Legacy job repository backed by media_job_state."""

    COLUMNS = (
        "job_id",
        "post_uid",
        "stage",
        "status",
        "source",
        "source_chat_id",
        "ch_name",
        "job_type",
        "message_count",
        "attempt",
        "last_error",
        "messages_json",
        "created_at",
        "updated_at",
    )

    def get(self, job_id: str) -> Optional[Mapping[str, Any]]:
        row = self.database.fetchone(
            """
            SELECT job_id, post_uid, stage, status, source, source_chat_id,
                   ch_name, job_type, message_count, attempt, last_error,
                   messages_json, created_at, updated_at
            FROM media_job_state WHERE job_id=?
            """,
            (str(job_id),),
        )
        value = _mapping(row, self.COLUMNS)
        if value is not None:
            with suppress_json_error():
                value["messages"] = json.loads(value.get("messages_json") or "[]")
        return value

    def upsert(self, job: Mapping[str, Any]) -> None:
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            raise DatabaseError("Job requires job_id")
        now = str(job.get("updated_at") or job.get("created_at") or "")
        values = {
            "post_uid": str(job.get("post_uid") or ""),
            "stage": str(job.get("stage") or ""),
            "status": str(job.get("status") or "queued"),
            "source": str(job.get("source") or ""),
            "source_chat_id": str(job.get("source_chat_id") or ""),
            "ch_name": str(job.get("ch_name") or ""),
            "job_type": str(job.get("job_type") or job.get("type") or ""),
            "message_count": int(job.get("message_count") or 0),
            "attempt": int(job.get("attempt") or 1),
            "last_error": str(job.get("last_error") or "")[:1000],
            "messages_json": json.dumps(
                job.get("messages") or [],
                ensure_ascii=True,
                sort_keys=True,
                default=str,
            ),
            "updated_at": now,
        }
        with self.database.transaction():
            current = self.get(job_id)
            if current is None:
                self.database.insert(
                    "media_job_state",
                    {
                        "job_id": job_id,
                        "created_at": str(job.get("created_at") or now),
                        **values,
                    },
                )
            else:
                self.database.update("media_job_state", values, {"job_id": job_id})

    def list_unfinished(self, limit: int = 1000) -> list[Mapping[str, Any]]:
        safe_limit = max(1, min(100000, int(limit)))
        rows = self.database.fetchall(
            """
            SELECT job_id, post_uid, stage, status, source, source_chat_id,
                   ch_name, job_type, message_count, attempt, last_error,
                   messages_json, created_at, updated_at
            FROM media_job_state
            WHERE status NOT IN (
                'uploaded', 'uploaded_partial', 'completed', 'failed',
                'skipped_duplicate', 'skipped_dead_media'
            )
            ORDER BY updated_at, job_id
            LIMIT ?
            """,
            (safe_limit,),
        )
        return [_mapping(row, self.COLUMNS) or {} for row in rows]

    def transition(
        self,
        job_id: str,
        *,
        stage: str,
        status: str,
        error: str = "",
    ) -> bool:
        return (
            self.database.update(
                "media_job_state",
                {
                    "stage": str(stage),
                    "status": str(status),
                    "last_error": str(error)[:1000],
                },
                {"job_id": str(job_id)},
            )
            > 0
        )


class suppress_json_error:
    """Small context manager used to keep legacy malformed rows readable."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type in {TypeError, ValueError, json.JSONDecodeError}


class QueueStateRepository(QueueStateRepositoryInterface):
    """Legacy queue repository backed by existing JSON state documents."""

    TERMINAL = frozenset(
        {
            "uploaded",
            "uploaded_partial",
            "completed",
            "failed",
            "skipped_duplicate",
            "skipped_dead_media",
        }
    )

    def __init__(self, runtime: RuntimeStateInterface) -> None:
        self.runtime = runtime

    @staticmethod
    def _state_name(queue_name: str) -> str:
        normalized = str(queue_name).strip().lower()
        if normalized not in {"download", "upload"}:
            raise DatabaseError(f"Unsupported legacy queue state: {queue_name}")
        return f"{normalized}_queue"

    def load(self, queue_name: str) -> Mapping[str, Any]:
        value = self.runtime.load(self._state_name(queue_name))
        return deepcopy(value or {"items": {}})

    def save(self, queue_name: str, state: Mapping[str, Any]) -> None:
        self.runtime.save(self._state_name(queue_name), dict(state))

    def list_pending(self, queue_name: str) -> list[Mapping[str, Any]]:
        state = self.load(queue_name)
        return [
            {"job_id": str(job_id), **dict(record)}
            for job_id, record in (state.get("items") or {}).items()
            if isinstance(record, Mapping)
            and str(record.get("status") or "") not in self.TERMINAL
        ]


class CursorRepository(CursorRepositoryInterface):
    """Legacy source cursor repository inside sync_source_manager.json."""

    def __init__(self, runtime: RuntimeStateInterface) -> None:
        self.runtime = runtime

    def _document(self) -> dict[str, Any]:
        return deepcopy(self.runtime.load("sync_source_manager") or {"cursors": {}})

    def get(self, source_id: str, cursor_kind: str) -> Optional[Mapping[str, Any]]:
        document = self._document()
        if cursor_kind == "historical":
            return deepcopy((document.get("cursors") or {}).get(str(source_id)))
        if cursor_kind == "hot":
            hot = document.get("startup_hot_scan") or {}
            return {
                "source_id": str(source_id),
                "completed": str(source_id) in set(hot.get("completed_sources") or []),
                "current_source": hot.get("current_source"),
                "payload": deepcopy(hot),
            }
        return deepcopy(
            (document.get("v20_cursors") or {})
            .get(str(cursor_kind), {})
            .get(str(source_id))
        )

    def upsert(
        self,
        source_id: str,
        cursor_kind: str,
        cursor: Mapping[str, Any],
    ) -> None:
        document = self._document()
        if cursor_kind == "historical":
            document.setdefault("cursors", {})[str(source_id)] = dict(cursor)
        else:
            document.setdefault("v20_cursors", {}).setdefault(
                str(cursor_kind), {}
            )[str(source_id)] = dict(cursor)
        self.runtime.save("sync_source_manager", document)

    def list_incomplete(self, cursor_kind: str) -> list[Mapping[str, Any]]:
        document = self._document()
        if cursor_kind == "historical":
            values = document.get("cursors") or {}
        else:
            values = (document.get("v20_cursors") or {}).get(cursor_kind, {})
        return [
            {"source_id": str(source_id), **dict(cursor)}
            for source_id, cursor in values.items()
            if isinstance(cursor, Mapping) and not bool(cursor.get("complete"))
        ]


class WorkerRepository(WorkerRepositoryInterface):
    """Sidecar worker state; legacy worker algorithms remain unchanged."""

    STATE_NAME = "v20_workers"

    def __init__(self, runtime: RuntimeStateInterface) -> None:
        self.runtime = runtime

    def _document(self) -> dict[str, Any]:
        return deepcopy(self.runtime.load(self.STATE_NAME) or {"workers": {}})

    def heartbeat(self, worker: Mapping[str, Any]) -> None:
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            raise DatabaseError("Worker heartbeat requires worker_id")
        document = self._document()
        document.setdefault("workers", {})[worker_id] = {
            **dict(worker),
            "heartbeat_at": float(worker.get("heartbeat_at") or time.time()),
        }
        self.runtime.save(self.STATE_NAME, document)

    def remove(self, worker_id: str) -> bool:
        document = self._document()
        removed = document.setdefault("workers", {}).pop(str(worker_id), None)
        if removed is not None:
            self.runtime.save(self.STATE_NAME, document)
        return removed is not None

    def list_live(self, max_age_seconds: float = 120.0) -> list[Mapping[str, Any]]:
        cutoff = time.time() - max(1.0, float(max_age_seconds))
        return [
            deepcopy(worker)
            for worker in self._document().get("workers", {}).values()
            if float(worker.get("heartbeat_at") or 0.0) >= cutoff
        ]


class CheckpointRepository(CheckpointRepositoryInterface):
    """Compatibility repository over RuntimeStateInterface checkpoints."""

    def __init__(self, runtime: RuntimeStateInterface) -> None:
        self.runtime = runtime

    def latest(self, name: str = "runtime_checkpoint") -> Optional[Mapping[str, Any]]:
        value = self.runtime.restore(name)
        return None if value is None else {"name": name, "payload": value}

    def save(self, name: str, checkpoint: Mapping[str, Any]) -> None:
        self.runtime.checkpoint(dict(checkpoint), name)


class AuditRepository(_Repository, AuditRepositoryInterface):
    """Append-only audit table created lazily for compatibility releases."""

    def _ensure(self) -> None:
        self.database.execute(
            """
            CREATE TABLE IF NOT EXISTS v20_audit_events (
                event_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )

    def append(
        self,
        action: str,
        *,
        object_type: str,
        object_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        self._ensure()
        event_id = f"audit_{uuid.uuid4().hex}"
        self.database.insert(
            "v20_audit_events",
            {
                "event_id": event_id,
                "action": str(action),
                "object_type": str(object_type),
                "object_id": str(object_id),
                "metadata_json": json.dumps(
                    dict(metadata or {}),
                    ensure_ascii=True,
                    sort_keys=True,
                    default=str,
                ),
                "created_at": str(time.time()),
            },
        )
        return event_id

    def latest(self, limit: int = 100) -> list[Mapping[str, Any]]:
        self._ensure()
        rows = self.database.fetchall(
            """
            SELECT event_id, action, object_type, object_id, metadata_json,
                   created_at
            FROM v20_audit_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, min(10000, int(limit))),),
        )
        return [
            _mapping(
                row,
                (
                    "event_id",
                    "action",
                    "object_type",
                    "object_id",
                    "metadata_json",
                    "created_at",
                ),
            )
            or {}
            for row in rows
        ]
