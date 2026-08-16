"""Exact adapters around the active Royells v19 compatibility runtime."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..errors import CheckpointError, DatabaseError, QueueError, RecoveryError
from ..interfaces import (
    CheckpointInterface,
    DatabaseInterface,
    DeliveryIntentInterface,
    LoggerInterface,
    MetricsInterface,
    QueueInterface,
    QueueRegistryInterface,
    RecoveryInterface,
    RuntimeStateInterface,
)
from ..models import (
    CheckpointRecord,
    DeliveryIntentRecord,
    QueueItem,
    RecoveryReport,
)
from .json_runtime import JsonRuntimeAdapter


@dataclass(frozen=True)
class LegacyDatabaseResult:
    """Detached SQLite result metadata."""

    rowcount: int
    lastrowid: int = 0


class LegacyDatabaseAdapter(DatabaseInterface):
    """Use the monolith's hardened SQLite connection and recovery gate."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self._local = threading.local()

    def connect(self) -> None:
        self.bridge.ensure_sqlite_runtime_ready(
            "v20 database adapter", attempts=3, require_write=True
        )

    def disconnect(self) -> None:
        self._release()

    def _active(self) -> Any:
        return getattr(self._local, "connection", None)

    def begin(self) -> None:
        depth = int(getattr(self._local, "depth", 0))
        if depth == 0:
            self.bridge.db_mutex.acquire()
            try:
                connection = self.bridge.db_connect()
                connection.execute("BEGIN IMMEDIATE")
            except Exception:
                self.bridge.db_mutex.release()
                raise
            self._local.connection = connection
        self._local.depth = depth + 1

    def commit(self) -> None:
        depth = int(getattr(self._local, "depth", 0))
        if depth <= 0:
            raise DatabaseError("No active legacy database transaction")
        depth -= 1
        self._local.depth = depth
        if depth == 0:
            connection = self._active()
            try:
                connection.commit()
            except Exception as exc:
                raise DatabaseError(str(exc)) from exc
            finally:
                self._release()

    def rollback(self) -> None:
        if int(getattr(self._local, "depth", 0)) <= 0:
            raise DatabaseError("No active legacy database transaction")
        connection = self._active()
        try:
            connection.rollback()
        finally:
            self._release()

    def _release(self) -> None:
        connection = self._active()
        self._local.connection = None
        self._local.depth = 0
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
            with contextlib.suppress(Exception):
                self.bridge.db_mutex.release()

    class _Transaction:
        def __init__(self, database: "LegacyDatabaseAdapter") -> None:
            self.database = database

        def __enter__(self) -> "LegacyDatabaseAdapter":
            self.database.begin()
            return self.database

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            if exc_type is None:
                self.database.commit()
            else:
                self.database.rollback()
            return False

    def transaction(self) -> "LegacyDatabaseAdapter._Transaction":
        return self._Transaction(self)

    def _run(
        self,
        statement: str,
        parameters: Sequence[Any] = (),
        *,
        fetch: str = "",
        many: Optional[Sequence[Sequence[Any]]] = None,
    ) -> Any:
        active = self._active()
        if active is not None:
            try:
                cursor = (
                    active.executemany(statement, many)
                    if many is not None
                    else active.execute(statement, tuple(parameters))
                )
                if fetch == "one":
                    return cursor.fetchone()
                if fetch == "all":
                    return list(cursor.fetchall())
                return LegacyDatabaseResult(
                    rowcount=int(cursor.rowcount),
                    lastrowid=int(cursor.lastrowid or 0),
                )
            except Exception as exc:
                raise DatabaseError(str(exc)) from exc
        with self.bridge.db_mutex:
            connection = self.bridge.db_connect()
            try:
                cursor = (
                    connection.executemany(statement, many)
                    if many is not None
                    else connection.execute(statement, tuple(parameters))
                )
                if fetch == "one":
                    return cursor.fetchone()
                if fetch == "all":
                    return list(cursor.fetchall())
                connection.commit()
                return LegacyDatabaseResult(
                    rowcount=int(cursor.rowcount),
                    lastrowid=int(cursor.lastrowid or 0),
                )
            except Exception as exc:
                with contextlib.suppress(Exception):
                    connection.rollback()
                if self.bridge.is_sqlite_corruption_error(exc):
                    self.bridge.request_db_online_recovery(
                        f"v20 adapter operation failed: {exc}"
                    )
                raise DatabaseError(str(exc)) from exc
            finally:
                connection.close()

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        return self._run(statement, parameters)

    def executemany(
        self, statement: str, parameter_sets: Sequence[Sequence[Any]]
    ) -> Any:
        return self._run(statement, many=parameter_sets)

    def fetchone(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        return self._run(statement, parameters, fetch="one")

    def fetchall(self, statement: str, parameters: Sequence[Any] = ()) -> list[Any]:
        return self._run(statement, parameters, fetch="all")

    @staticmethod
    def _identifier(value: str) -> str:
        normalized = str(value)
        if not normalized.replace("_", "").isalnum() or normalized[0].isdigit():
            raise DatabaseError(f"Unsafe SQL identifier: {value!r}")
        return f'"{normalized}"'

    def insert(self, table: str, values: Mapping[str, Any]) -> int:
        if not values:
            raise DatabaseError("Insert requires values")
        columns = list(values)
        result = self.execute(
            f"INSERT INTO {self._identifier(table)} "
            f"({', '.join(self._identifier(item) for item in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
        return int(result.lastrowid or result.rowcount)

    def update(
        self,
        table: str,
        values: Mapping[str, Any],
        where: Mapping[str, Any],
    ) -> int:
        if not values or not where:
            raise DatabaseError("Update requires values and where")
        value_columns = list(values)
        where_columns = list(where)
        result = self.execute(
            f"UPDATE {self._identifier(table)} SET "
            + ", ".join(f"{self._identifier(column)}=?" for column in value_columns)
            + " WHERE "
            + " AND ".join(
                f"{self._identifier(column)}=?" for column in where_columns
            ),
            [values[column] for column in value_columns]
            + [where[column] for column in where_columns],
        )
        return int(result.rowcount)

    def delete(self, table: str, where: Mapping[str, Any]) -> int:
        if not where:
            raise DatabaseError("Delete requires where")
        columns = list(where)
        result = self.execute(
            f"DELETE FROM {self._identifier(table)} WHERE "
            + " AND ".join(f"{self._identifier(column)}=?" for column in columns),
            [where[column] for column in columns],
        )
        return int(result.rowcount)

    def health(self) -> Mapping[str, Any]:
        status = self.bridge.db_integrity_status()
        return {
            "ok": status == "ok" and not bool(self.bridge.DB_RECOVERY_ACTIVE),
            "integrity": status,
            "degraded": bool(self.bridge.DB_DEGRADED),
            "recovery_active": bool(self.bridge.DB_RECOVERY_ACTIVE),
            "last_ready_at": float(self.bridge.DB_LAST_READY_AT or 0.0),
        }

    def ping(self) -> float:
        started = time.perf_counter()
        if self.fetchone("SELECT 1") is None:
            raise DatabaseError("Legacy SQLite ping failed")
        return time.perf_counter() - started

    def backup(self, destination: str | os.PathLike[str]) -> None:
        destination_path = Path(destination).expanduser()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with self.bridge.db_mutex:
            source = self.bridge.db_connect()
            target = self.bridge.sqlite3.connect(str(destination_path), timeout=60)
            try:
                source.backup(target)
                target.commit()
            finally:
                source.close()
                target.close()
        if not self._validate_file(destination_path):
            raise DatabaseError("Legacy SQLite backup failed integrity validation")

    def restore(self, source: str | os.PathLike[str]) -> None:
        source_path = Path(source).expanduser()
        if not self._validate_file(source_path):
            raise DatabaseError("Restore source failed SQLite integrity validation")
        self.bridge.reset_sqlite_open_state("v20 adapter restore", remove_sidecars=True)
        temporary = Path(self.bridge.DB_FILE).with_suffix(".v20-restore.tmp")
        shutil.copy2(source_path, temporary)
        os.replace(temporary, self.bridge.DB_FILE)
        self.bridge.ensure_sqlite_runtime_ready(
            "v20 adapter restore", attempts=5, require_write=True
        )

    def close(self) -> None:
        self.disconnect()

    def _validate_file(self, path: Path) -> bool:
        connection = None
        try:
            connection = self.bridge.sqlite3.connect(str(path), timeout=30)
            result = connection.execute("PRAGMA integrity_check").fetchone()
            return bool(result and str(result[0]).strip().lower() == "ok")
        except Exception:
            return False
        finally:
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()


class LegacyQueueAdapter(QueueInterface):
    """QueueInterface over one active in-memory legacy queue."""

    def __init__(self, bridge: Any, name: str, queue_object: Any) -> None:
        self.bridge = bridge
        self.name = str(name)
        self.queue = queue_object
        self._lock = asyncio.Lock()
        self._reserved: dict[str, tuple[str, Any, float]] = {}

    @staticmethod
    def _item_id(payload: Mapping[str, Any], fallback: Optional[str] = None) -> str:
        return str(
            payload.get("job_id")
            or payload.get("intent_id")
            or payload.get("url")
            or fallback
            or uuid.uuid4().hex
        )

    async def enqueue(
        self,
        payload: Mapping[str, Any],
        *,
        item_id: Optional[str] = None,
        priority: int = 0,
        available_at: Optional[float] = None,
    ) -> str:
        if not isinstance(payload, Mapping):
            raise QueueError("Legacy queue payload must be a mapping")
        job = dict(payload)
        stable_id = self._item_id(job, item_id)
        job.setdefault("job_id", stable_id)
        job.setdefault("_v20_priority", int(priority))
        if available_at and available_at > time.time():
            delay = max(0.0, float(available_at) - time.time())
            if hasattr(self.bridge, "schedule_queue_retry") and self.name in {
                "download",
                "upload",
                "link",
            }:
                if not self.bridge.schedule_queue_retry(self.name, job, delay, "v20 enqueue"):
                    raise QueueError(f"Legacy {self.name} retry queue is full")
                return stable_id
            await asyncio.sleep(delay)
        await self.queue.put(job)
        return stable_id

    async def dequeue(
        self,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
        timeout: Optional[float] = None,
    ) -> Optional[QueueItem]:
        try:
            payload = (
                await self.queue.get()
                if timeout is None
                else await asyncio.wait_for(self.queue.get(), timeout=max(0.0, timeout))
            )
        except asyncio.TimeoutError:
            return None
        if not isinstance(payload, Mapping):
            self.queue.task_done()
            raise QueueError(f"Legacy {self.name} queue returned a non-mapping item")
        item_id = self._item_id(payload)
        lease_until = time.time() + max(1.0, float(visibility_timeout))
        async with self._lock:
            self._reserved[item_id] = (str(worker_id), payload, lease_until)
        return QueueItem(
            item_id=item_id,
            payload=dict(payload),
            priority=int(payload.get("_v20_priority") or 0),
            attempts=int(payload.get("attempt") or payload.get("retries") or 0),
            status="reserved",
            reserved_by=str(worker_id),
            lease_until=lease_until,
            available_at=time.time(),
            created_at=float(payload.get("_queued_at_monotonic") or time.time()),
            updated_at=time.time(),
            last_error=str(payload.get("last_error") or ""),
        )

    async def peek(self) -> Optional[QueueItem]:
        raw_queue = getattr(self.queue, "_queue", self.queue)
        items = list(getattr(raw_queue, "_queue", ()))
        if not items:
            return None
        candidate = items[0]
        if isinstance(candidate, tuple) and len(candidate) >= 3:
            candidate = candidate[2]
        if not isinstance(candidate, Mapping):
            return None
        return QueueItem(
            item_id=self._item_id(candidate),
            payload=dict(candidate),
            priority=int(candidate.get("_v20_priority") or 0),
            status="pending",
        )

    async def ack(self, item_id: str, worker_id: Optional[str] = None) -> bool:
        async with self._lock:
            reservation = self._reserved.get(str(item_id))
            if reservation is None:
                return False
            if worker_id is not None and reservation[0] != str(worker_id):
                return False
            self._reserved.pop(str(item_id), None)
        self.queue.task_done()
        return True

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
        async with self._lock:
            reservation = self._reserved.get(str(item_id))
            if reservation is None:
                return False
            if worker_id is not None and reservation[0] != str(worker_id):
                return False
            self._reserved.pop(str(item_id), None)
        payload = dict(reservation[1])
        payload["last_error"] = str(error)[:500]
        self.queue.task_done()
        if self.name in {"download", "upload", "link"} and hasattr(
            self.bridge, "schedule_queue_retry"
        ):
            scheduled = bool(
                self.bridge.schedule_queue_retry(
                    self.name, payload, max(0.0, delay_seconds), error
                )
            )
            if scheduled:
                return True
            # The retry scheduler can reject during pressure or shutdown.
            # Requeue locally so a failed scheduling attempt never loses work.
            await self.queue.put(payload)
            return True
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await self.queue.put(payload)
        return True

    async def size(self) -> int:
        return int(self.queue.qsize()) + len(self._reserved)

    async def clear(self) -> int:
        removed = 0
        queue = getattr(self.queue, "_queue", self.queue)
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                del item
                self.queue.task_done()
                removed += 1
        async with self._lock:
            reserved = len(self._reserved)
            self._reserved.clear()
        for _ in range(reserved):
            self.queue.task_done()
        return removed + reserved

    async def reserve(
        self,
        item_id: str,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
    ) -> bool:
        async with self._lock:
            reservation = self._reserved.get(str(item_id))
            if reservation is None:
                return False
            if reservation[0] != str(worker_id) and reservation[2] > time.time():
                return False
            self._reserved[str(item_id)] = (
                str(worker_id),
                reservation[1],
                time.time() + max(1.0, float(visibility_timeout)),
            )
            return True

    async def heartbeat(
        self,
        item_id: str,
        *,
        worker_id: str,
        visibility_timeout: float = 300.0,
    ) -> bool:
        return await self.reserve(
            item_id,
            worker_id=worker_id,
            visibility_timeout=visibility_timeout,
        )

    async def metrics(self) -> Mapping[str, Any]:
        async with self._lock:
            reservations = len(self._reserved)
            expired = sum(
                1 for _owner, _payload, lease in self._reserved.values()
                if lease <= time.time()
            )
        return {
            "queue": self.name,
            "pending": int(self.queue.qsize()),
            "reserved": reservations,
            "expired_reservations": expired,
            "capacity": int(getattr(self.queue, "maxsize", 0) or 0),
        }


class LegacyQueueRegistry(QueueRegistryInterface):
    """Canonical named access to every active legacy queue."""

    def __init__(self, bridge: Any) -> None:
        queues = {
            "download": bridge.channel_download_queue,
            "upload": bridge.upload_queue,
            "link": bridge.link_process_queue,
            "button": bridge.button_queue,
            "job_state_db": bridge.job_state_db_queue,
            "critical_db": bridge.critical_db_write_queue,
        }
        self._queues = {
            name: LegacyQueueAdapter(bridge, name, queue)
            for name, queue in queues.items()
        }

    def get(self, name: str) -> QueueInterface:
        normalized = str(name).strip().lower()
        if normalized not in self._queues:
            raise QueueError(f"Unknown legacy queue: {name}")
        return self._queues[normalized]

    def names(self) -> Sequence[str]:
        return tuple(self._queues)

    async def metrics(self) -> Mapping[str, Any]:
        values = await asyncio.gather(
            *(self._queues[name].metrics() for name in self._queues)
        )
        return dict(zip(self._queues, values))


class LegacyRuntimeAdapter(RuntimeStateInterface):
    """Expose current state JSON while keeping v20 sidecar state isolated."""

    def __init__(self, bridge: Any, root: str | os.PathLike[str]) -> None:
        self.bridge = bridge
        self.sidecar = JsonRuntimeAdapter(Path(root) / ".v20_state")

    def load(self, name: str = "runtime_checkpoint") -> Any:
        if name == "runtime_checkpoint":
            return self.bridge.load_runtime_checkpoint_sync()[0]
        if name in self.bridge.STATE:
            with self.bridge.state_mutex:
                return deepcopy(self.bridge.STATE[name])
        return self.sidecar.load(name)

    def save(self, name: str, payload: Any) -> int:
        if name in self.bridge.STATE:
            with self.bridge.state_mutex:
                self.bridge.STATE[name] = deepcopy(payload)
                self.bridge.save_state(name)
            return int(time.time() * 1000)
        return self.sidecar.save(name, payload)

    def delete(self, name: str) -> None:
        if name in self.bridge.STATE:
            raise CheckpointError(f"Refusing to delete authoritative legacy state: {name}")
        self.sidecar.delete(name)

    def checkpoint(self, payload: Any, name: str = "runtime_checkpoint") -> int:
        if name == "runtime_checkpoint":
            return self.sidecar.checkpoint(payload, "compatibility_checkpoint")
        return self.save(name, payload)

    def restore(self, name: str = "runtime_checkpoint") -> Any:
        if name == "runtime_checkpoint":
            return self.bridge.load_runtime_checkpoint_sync()[0]
        if name in self.bridge.STATE:
            return self.load(name)
        return self.sidecar.restore(name)

    def heartbeat(
        self, component: str, metadata: Optional[Mapping[str, Any]] = None
    ) -> int:
        return self.sidecar.heartbeat(component, metadata)

    def lock(self, name: str = "runtime") -> str:
        return self.sidecar.lock(name)

    def unlock(self, token: str) -> None:
        self.sidecar.unlock(token)


class LegacyCheckpointAdapter(CheckpointInterface):
    """Delegate checkpoint capture to the production zero-loss implementation."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    @staticmethod
    def _checksum(payload: Any) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def load(self, name: str = "runtime_checkpoint") -> Optional[CheckpointRecord]:
        if name != "runtime_checkpoint":
            raise CheckpointError(f"Unknown legacy checkpoint: {name}")
        payload, source = self.bridge.load_runtime_checkpoint_sync()
        if not payload:
            return None
        return CheckpointRecord(
            name=name,
            schema_version=int(self.bridge.RUNTIME_CHECKPOINT_SCHEMA_VERSION),
            revision=int(self.bridge.RUNTIME_CHECKPOINT_SEQUENCE),
            written_at=str(payload.get("checkpointed_at") or ""),
            payload=payload,
            sha256=self._checksum(payload),
            source=source,
        )

    async def capture(
        self,
        reason: str,
        *,
        force: bool = False,
        name: str = "runtime_checkpoint",
    ) -> CheckpointRecord:
        if name != "runtime_checkpoint":
            raise CheckpointError(f"Unknown legacy checkpoint: {name}")
        await self.bridge.checkpoint_runtime_state(reason, force=force)
        record = self.load(name)
        if record is None:
            raise CheckpointError("Legacy checkpoint capture returned no generation")
        return record

    def restore(self, name: str = "runtime_checkpoint") -> Optional[CheckpointRecord]:
        return self.load(name)

    def validate(self, record: CheckpointRecord) -> bool:
        return (
            record.name == "runtime_checkpoint"
            and record.schema_version <= int(self.bridge.RUNTIME_CHECKPOINT_SCHEMA_VERSION)
            and record.sha256 == self._checksum(record.payload)
            and isinstance(record.payload, Mapping)
        )

    def status(self) -> Mapping[str, Any]:
        return {
            "enabled": bool(self.bridge.RUNTIME_CHECKPOINT_ENABLED),
            "sequence": int(self.bridge.RUNTIME_CHECKPOINT_SEQUENCE),
            "last_written_at": float(self.bridge.RUNTIME_CHECKPOINT_LAST_WRITTEN),
            "last_reason": str(self.bridge.RUNTIME_CHECKPOINT_LAST_REASON),
            "dirty": bool(self.bridge.RUNTIME_CHECKPOINT_DIRTY),
            "loaded_source": str(self.bridge.RUNTIME_CHECKPOINT_LOADED_SOURCE),
        }


class LegacyRecoveryAdapter(RecoveryInterface):
    """Use the existing queue and delivery reconciliation implementation."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self._status: dict[str, Any] = {
            "status": "not_run",
            "reason": "",
            "updated_at": 0.0,
        }

    async def recover(self, *, reason: str = "startup") -> RecoveryReport:
        try:
            delivery = await self.bridge.recover_delivery_intents_after_restart()
            queue = await self.bridge.recover_queue_state()
            report = RecoveryReport(
                recovered=int(queue.get("recovered") or 0),
                deferred=int(queue.get("deferred") or 0),
                duplicates=int(queue.get("duplicates") or 0),
                failed=int(queue.get("failed") or 0),
                pending=int(queue.get("pending") or 0),
                consistent=not bool(queue.get("busy")),
                details={"queue": queue, "delivery": delivery, "reason": reason},
            )
            self._status = {
                "status": "ok" if report.consistent else "deferred",
                "reason": str(reason),
                "updated_at": time.time(),
                "report": report.details,
            }
            return report
        except Exception as exc:
            self._status = {
                "status": "failed",
                "reason": str(reason),
                "updated_at": time.time(),
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
            raise RecoveryError(str(exc)) from exc

    async def validate(self) -> RecoveryReport:
        pending = len(self.bridge.persisted_recovery_pending_job_ids())
        unresolved = len(self.bridge.delivery_intents_snapshot())
        return RecoveryReport(
            pending=pending,
            deferred=unresolved,
            consistent=pending == 0 and unresolved == 0,
            details={
                "pending_queue_jobs": pending,
                "pending_delivery_intents": unresolved,
                "reservations": self.bridge.persisted_queue_reservations.owner_count(),
            },
        )

    def status(self) -> Mapping[str, Any]:
        return deepcopy(self._status)


class LegacyMetricsAdapter(MetricsInterface):
    """Expose the production metric registry through MetricsInterface."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    def increment(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        del labels
        self.bridge.metric_increment(name, amount)

    def decrement(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        self.increment(name, -float(amount), **labels)

    def observe(self, name: str, value: float, **labels: Any) -> None:
        label = ":".join(f"{key}={labels[key]}" for key in sorted(labels))
        key = f"{name}:{label}" if label else name
        with self.bridge.RUNTIME_METRICS_LOCK:
            bucket = self.bridge.RUNTIME_METRICS.setdefault(
                "v20_observations", {}
            ).setdefault(key, {"count": 0, "sum": 0.0, "max": 0.0})
            bucket["count"] += 1
            bucket["sum"] += float(value)
            bucket["max"] = max(float(bucket["max"]), float(value))

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        del labels
        self.bridge.metric_set(name, value)

    class _Timer:
        def __init__(self, metrics: "LegacyMetricsAdapter", name: str, labels: Any) -> None:
            self.metrics = metrics
            self.name = name
            self.labels = labels
            self.started = 0.0

        def __enter__(self) -> None:
            self.started = time.perf_counter()
            return None

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            self.metrics.observe(
                self.name, time.perf_counter() - self.started, **self.labels
            )
            return False

    def timer(self, name: str, **labels: Any) -> "LegacyMetricsAdapter._Timer":
        return self._Timer(self, name, labels)

    def snapshot(self) -> Mapping[str, Any]:
        return self.bridge.runtime_metrics_snapshot()


class LegacyLoggerAdapter(LoggerInterface):
    """Route structured v20 events through the production log pipeline."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    @staticmethod
    def _render(message: str, fields: Mapping[str, Any]) -> str:
        safe = {
            key: value
            for key, value in fields.items()
            if key.lower()
            not in {"password", "token", "secret", "session", "api_hash"}
        }
        if not safe:
            return str(message)
        return (
            f"{message} | "
            + json.dumps(
                safe,
                ensure_ascii=True,
                sort_keys=True,
                default=str,
            )
        )

    def info(self, message: str, **fields: Any) -> None:
        self.bridge.log_event(self._render(message, fields))

    def warn(self, message: str, **fields: Any) -> None:
        self.bridge.log_event("WARNING: " + self._render(message, fields))

    def error(self, message: str, **fields: Any) -> None:
        self.bridge.log_event("ERROR: " + self._render(message, fields))

    def performance(self, message: str, **fields: Any) -> None:
        self.bridge.log_event("PERFORMANCE: " + self._render(message, fields))

    def audit(self, message: str, **fields: Any) -> None:
        self.bridge.log_event("AUDIT: " + self._render(message, fields))


class LegacyDeliveryIntentRepository(DeliveryIntentInterface):
    """Synchronous repository over the existing fsynced delivery ledger."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    @staticmethod
    def _from_dict(value: Mapping[str, Any]) -> DeliveryIntentRecord:
        messages = value.get("messages") or []
        media_uids = [
            str(item.get("media_uid") or "")
            for item in messages
            if isinstance(item, Mapping) and item.get("media_uid")
        ]
        return DeliveryIntentRecord(
            intent_id=str(value.get("intent_id") or ""),
            job_id=str(value.get("job_id") or ""),
            operation=str(value.get("operation") or ""),
            status=str(value.get("status") or "pending"),
            media_uids=tuple(media_uids),
            target_message_ids=tuple(
                int(item) for item in value.get("target_message_ids") or []
            ),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            last_error=str(value.get("last_error") or ""),
            metadata={
                key: deepcopy(item)
                for key, item in value.items()
                if key
                not in {
                    "intent_id",
                    "job_id",
                    "operation",
                    "status",
                    "messages",
                    "target_message_ids",
                    "created_at",
                    "updated_at",
                    "last_error",
                }
            },
        )

    def _persist(self) -> None:
        self.bridge._persist_delivery_intents_sync()

    def create(self, intent: DeliveryIntentRecord) -> DeliveryIntentRecord:
        value = {
            "intent_id": intent.intent_id,
            "job_id": intent.job_id,
            "operation": intent.operation,
            "status": intent.status,
            "messages": [
                {"media_uid": uid} for uid in intent.media_uids
            ],
            "target_chat_id": intent.target_chat_id,
            "target_message_ids": list(intent.target_message_ids),
            "attempt": int(intent.attempt),
            "created_at": intent.created_at or self.bridge.now_iso(),
            "updated_at": intent.updated_at or self.bridge.now_iso(),
            "last_error": intent.last_error,
            **dict(intent.metadata),
        }
        with self.bridge.DELIVERY_INTENTS_LOCK:
            current = self.bridge.DELIVERY_INTENTS.setdefault(intent.intent_id, value)
        self._persist()
        return self._from_dict(current)

    def get(self, intent_id: str) -> Optional[DeliveryIntentRecord]:
        with self.bridge.DELIVERY_INTENTS_LOCK:
            value = deepcopy(self.bridge.DELIVERY_INTENTS.get(str(intent_id)))
        return None if value is None else self._from_dict(value)

    def pending(self, job_id: Optional[str] = None) -> Sequence[DeliveryIntentRecord]:
        with self.bridge.DELIVERY_INTENTS_LOCK:
            values = deepcopy(list(self.bridge.DELIVERY_INTENTS.values()))
        if job_id is not None:
            values = [
                value for value in values
                if str(value.get("job_id") or "") == str(job_id)
            ]
        return tuple(self._from_dict(value) for value in values)

    def _update(self, intent_id: str, **values: Any) -> DeliveryIntentRecord:
        with self.bridge.DELIVERY_INTENTS_LOCK:
            intent = self.bridge.DELIVERY_INTENTS.get(str(intent_id))
            if intent is None:
                raise CheckpointError(f"Unknown delivery intent: {intent_id}")
            intent.update(values)
            intent["updated_at"] = self.bridge.now_iso()
            result = deepcopy(intent)
        self._persist()
        return self._from_dict(result)

    def mark_accepted(
        self,
        intent_id: str,
        target_message_ids: Sequence[int],
        *,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> DeliveryIntentRecord:
        return self._update(
            intent_id,
            status="accepted",
            target_message_ids=[int(value) for value in target_message_ids],
            **dict(metadata or {}),
        )

    def complete(self, intent_id: str) -> DeliveryIntentRecord:
        current = self.get(intent_id)
        if current is None:
            raise CheckpointError(f"Unknown delivery intent: {intent_id}")
        with self.bridge.DELIVERY_INTENTS_LOCK:
            self.bridge.DELIVERY_INTENTS.pop(str(intent_id), None)
        self._persist()
        return DeliveryIntentRecord(**{**current.__dict__, "status": "completed"})

    def fail(self, intent_id: str, error: str) -> DeliveryIntentRecord:
        return self._update(intent_id, status="failed", last_error=str(error)[:1000])
