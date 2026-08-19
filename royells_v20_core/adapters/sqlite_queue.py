"""Durable SQLite queue adapter for Book 17.

The production v19 queue is a composite of in-memory queues, JSON records,
runtime checkpoints, reservations, and media_job_state.  This adapter is a
new isolated queue implementation for the later migration path; it is never
opened by the current production entry point.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from ..errors import QueueError
from ..interfaces import QueueInterface
from ..models import QueueItem


class SQLiteQueueAdapter(QueueInterface):
    """Priority queue with atomic reservations and visibility timeouts."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        queue_name: str = "default",
        timeout: float = 30.0,
    ) -> None:
        self.path = Path(path).expanduser()
        self.queue_name = str(queue_name or "default")
        self.timeout = max(0.1, float(timeout))
        self._lock = threading.RLock()
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.timeout,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return connection

    def _initialize(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.closing(self._connection()) as connection:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS book17_queue_items (
                            queue_name TEXT NOT NULL,
                            item_id TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            priority INTEGER NOT NULL DEFAULT 0,
                            available_at REAL NOT NULL,
                            status TEXT NOT NULL DEFAULT 'pending',
                            reserved_by TEXT,
                            lease_until REAL,
                            attempts INTEGER NOT NULL DEFAULT 0,
                            last_error TEXT NOT NULL DEFAULT '',
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            PRIMARY KEY (queue_name, item_id)
                        )
                        """
                    )
                    connection.commit()
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_book17_queue_ready
                        ON book17_queue_items
                        (queue_name, status, available_at, priority DESC, created_at)
                        """
                    )
                    connection.commit()
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue initialization failed") from exc

    def _row_to_item(self, row: sqlite3.Row) -> QueueItem:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise QueueError(f"Queue payload is invalid: {row['item_id']}") from exc
        if not isinstance(payload, dict):
            raise QueueError(f"Queue payload must be an object: {row['item_id']}")
        return QueueItem(
            item_id=str(row["item_id"]),
            payload=payload,
            priority=int(row["priority"]),
            attempts=int(row["attempts"]),
            status=str(row["status"]),
            reserved_by=row["reserved_by"],
            lease_until=row["lease_until"],
            available_at=float(row["available_at"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_error=str(row["last_error"] or ""),
        )

    def _claim(
        self,
        *,
        item_id: Optional[str],
        worker_id: str,
        visibility_timeout: float,
    ) -> Optional[QueueItem]:
        now = time.time()
        lease_until = now + max(1.0, float(visibility_timeout))
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if item_id is None:
                        row = connection.execute(
                            """
                            SELECT *
                            FROM book17_queue_items
                            WHERE queue_name=?
                              AND available_at<=?
                              AND (
                                status='pending'
                                OR (status='reserved' AND lease_until<=?)
                              )
                            ORDER BY priority DESC, available_at ASC, created_at ASC
                            LIMIT 1
                            """,
                            (self.queue_name, now, now),
                        ).fetchone()
                    else:
                        row = connection.execute(
                            """
                            SELECT *
                            FROM book17_queue_items
                            WHERE queue_name=? AND item_id=?
                              AND (
                                (status='pending' AND available_at<=?)
                                OR (status='reserved' AND lease_until<=?)
                              )
                            """,
                            (self.queue_name, str(item_id), now, now),
                        ).fetchone()
                    if row is None:
                        connection.commit()
                        return None
                    connection.execute(
                        """
                        UPDATE book17_queue_items
                        SET status='reserved', reserved_by=?, lease_until=?,
                            attempts=attempts+1, updated_at=?
                        WHERE queue_name=? AND item_id=?
                        """,
                        (worker_id, lease_until, now, self.queue_name, row["item_id"]),
                    )
                    updated = connection.execute(
                        """
                        SELECT * FROM book17_queue_items
                        WHERE queue_name=? AND item_id=?
                        """,
                        (self.queue_name, row["item_id"]),
                    ).fetchone()
                    connection.commit()
                    return self._row_to_item(updated)
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue reservation failed") from exc

    async def enqueue(
        self,
        payload: Mapping[str, Any],
        *,
        item_id: Optional[str] = None,
        priority: int = 0,
        available_at: Optional[float] = None,
    ) -> str:
        if not isinstance(payload, Mapping):
            raise QueueError("Queue payload must be a mapping")
        stable_id = str(item_id or uuid.uuid4().hex)
        now = time.time()
        ready_at = now if available_at is None else float(available_at)
        try:
            encoded = json.dumps(dict(payload), ensure_ascii=True, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise QueueError("Queue payload is not JSON serializable") from exc
        await asyncio.to_thread(
            self._enqueue_sync,
            stable_id,
            encoded,
            int(priority),
            ready_at,
            now,
        )
        return stable_id

    def _enqueue_sync(
        self,
        stable_id: str,
        encoded: str,
        priority: int,
        ready_at: float,
        now: float,
    ) -> None:
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO book17_queue_items
                        (queue_name, item_id, payload_json, priority, available_at,
                         status, attempts, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                        """,
                        (
                            self.queue_name,
                            stable_id,
                            encoded,
                            priority,
                            ready_at,
                            now,
                            now,
                        ),
                    )
                    connection.commit()
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue enqueue failed") from exc

    async def dequeue(
        self,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
        timeout: Optional[float] = None,
    ) -> Optional[QueueItem]:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            item = await asyncio.to_thread(
                self._claim,
                item_id=None,
                worker_id=worker_id,
                visibility_timeout=visibility_timeout,
            )
            if item is not None:
                return item
            if deadline is not None and time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def peek(self) -> Optional[QueueItem]:
        return await asyncio.to_thread(self._peek_sync)

    def _peek_sync(self) -> Optional[QueueItem]:
        now = time.time()
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    row = connection.execute(
                        """
                        SELECT *
                        FROM book17_queue_items
                        WHERE queue_name=? AND status='pending' AND available_at<=?
                        ORDER BY priority DESC, available_at ASC, created_at ASC
                        LIMIT 1
                        """,
                        (self.queue_name, now),
                    ).fetchone()
                    return None if row is None else self._row_to_item(row)
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue peek failed") from exc

    async def ack(self, item_id: str, worker_id: Optional[str] = None) -> bool:
        return await asyncio.to_thread(self._ack_sync, item_id, worker_id)

    def _ack_sync(self, item_id: str, worker_id: Optional[str]) -> bool:
        clauses = ["queue_name=?", "item_id=?", "status='reserved'"]
        params: list[Any] = [self.queue_name, str(item_id)]
        if worker_id is not None:
            clauses.append("reserved_by=?")
            params.append(worker_id)
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    cursor = connection.execute(
                        f"DELETE FROM book17_queue_items WHERE {' AND '.join(clauses)}",
                        params,
                    )
                    connection.commit()
                    return cursor.rowcount == 1
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue ack failed") from exc

    async def nack(
        self,
        item_id: str,
        *,
        worker_id: Optional[str] = None,
        delay_seconds: float = 0.0,
        error: str = "",
    ) -> bool:
        return await self._release(
            item_id,
            worker_id=worker_id,
            delay_seconds=delay_seconds,
            error=error,
        )

    async def retry(
        self,
        item_id: str,
        *,
        worker_id: Optional[str] = None,
        delay_seconds: float = 0.0,
        error: str = "",
    ) -> bool:
        return await self._release(
            item_id,
            worker_id=worker_id,
            delay_seconds=delay_seconds,
            error=error,
        )

    async def _release(
        self,
        item_id: str,
        *,
        worker_id: Optional[str],
        delay_seconds: float,
        error: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._release_sync,
            item_id,
            worker_id,
            delay_seconds,
            error,
        )

    def _release_sync(
        self,
        item_id: str,
        worker_id: Optional[str],
        delay_seconds: float,
        error: str,
    ) -> bool:
        clauses = ["queue_name=?", "item_id=?", "status='reserved'"]
        where_params: list[Any] = [self.queue_name, str(item_id)]
        if worker_id is not None:
            clauses.append("reserved_by=?")
            where_params.append(worker_id)
        now = time.time()
        parameters = [
            now + max(0.0, delay_seconds),
            str(error)[:1000],
            now,
            *where_params,
        ]
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    cursor = connection.execute(
                        f"""
                        UPDATE book17_queue_items
                        SET status='pending', reserved_by=NULL, lease_until=NULL,
                            available_at=?, last_error=?, updated_at=?
                        WHERE {' AND '.join(clauses)}
                        """,
                        parameters,
                    )
                    connection.commit()
                    return cursor.rowcount == 1
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue release failed") from exc

    async def size(self) -> int:
        return await asyncio.to_thread(self._size_sync)

    def _size_sync(self) -> int:
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    row = connection.execute(
                        """
                        SELECT COUNT(*) FROM book17_queue_items
                        WHERE queue_name=?
                        """,
                        (self.queue_name,),
                    ).fetchone()
                    return int(row[0])
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue size failed") from exc

    async def clear(self) -> int:
        return await asyncio.to_thread(self._clear_sync)

    def _clear_sync(self) -> int:
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    cursor = connection.execute(
                        "DELETE FROM book17_queue_items WHERE queue_name=?",
                        (self.queue_name,),
                    )
                    connection.commit()
                    return int(cursor.rowcount)
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue clear failed") from exc

    async def reserve(
        self,
        item_id: str,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
    ) -> bool:
        return (
            await asyncio.to_thread(
                self._claim,
                item_id=str(item_id),
                worker_id=worker_id,
                visibility_timeout=visibility_timeout,
            )
            is not None
        )

    async def heartbeat(
        self,
        item_id: str,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
    ) -> bool:
        return await asyncio.to_thread(
            self._heartbeat_sync,
            item_id,
            worker_id,
            visibility_timeout,
        )

    def _heartbeat_sync(
        self,
        item_id: str,
        worker_id: str,
        visibility_timeout: float,
    ) -> bool:
        now = time.time()
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    cursor = connection.execute(
                        """
                        UPDATE book17_queue_items
                        SET lease_until=?, updated_at=?
                        WHERE queue_name=? AND item_id=? AND status='reserved'
                          AND reserved_by=?
                        """,
                        (
                            now + max(1.0, float(visibility_timeout)),
                            now,
                            self.queue_name,
                            str(item_id),
                            worker_id,
                        ),
                    )
                    connection.commit()
                    return cursor.rowcount == 1
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue heartbeat failed") from exc

    async def metrics(self) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._metrics_sync)

    def _metrics_sync(self) -> Mapping[str, Any]:
        with self._lock:
            try:
                with contextlib.closing(self._connection()) as connection:
                    rows = connection.execute(
                        """
                        SELECT status, COUNT(*) AS count,
                               COALESCE(SUM(attempts), 0) AS attempts
                        FROM book17_queue_items
                        WHERE queue_name=?
                        GROUP BY status
                        """,
                        (self.queue_name,),
                    ).fetchall()
                    result = {"queue": self.queue_name, "total": 0, "statuses": {}}
                    for row in rows:
                        result["statuses"][str(row["status"])] = {
                            "count": int(row["count"]),
                            "attempts": int(row["attempts"]),
                        }
                        result["total"] += int(row["count"])
                    return result
            except (sqlite3.Error, OSError) as exc:
                raise QueueError("SQLite queue metrics failed") from exc
