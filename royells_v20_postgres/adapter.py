"""Psycopg 3 PostgreSQL adapter implementing Book 17 DatabaseInterface."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import hmac
import json
import os
import random
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence
from uuid import UUID

from royells_v20_core.errors import ConfigurationError, DatabaseError
from royells_v20_core.interfaces import DatabaseInterface

from .configuration import PostgresSettings
from .errors import (
    ConnectionFailure,
    ForeignKeyViolation,
    IntegrityFailure,
    PostgresError,
    PostgresTimeout,
    SerializationFailure,
    UniqueViolation,
)
from .sql import quote_identifier, split_sql_statements, translate_qmark


@dataclass(frozen=True)
class PostgresResult:
    """Detached query metadata safe after a pooled connection is returned."""

    rowcount: int
    status_message: str = ""


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    raise TypeError(f"Unsupported backup value: {type(value).__name__}")


def _json_object_hook(value: dict[str, Any]) -> Any:
    if set(value) == {"__bytes_hex__"}:
        try:
            return bytes.fromhex(str(value["__bytes_hex__"]))
        except ValueError as exc:
            raise IntegrityFailure("Backup contains invalid byte encoding") from exc
    return value


def _classify_error(exc: BaseException) -> PostgresError:
    name = exc.__class__.__name__
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    message = str(exc)[:1000]
    if name in {"PoolTimeout", "ConnectionTimeout", "QueryCanceled"}:
        return PostgresTimeout(message)
    if name in {
        "OperationalError",
        "InterfaceError",
        "ConnectionException",
        "AdminShutdown",
        "CannotConnectNow",
    } or sqlstate.startswith("08"):
        return ConnectionFailure(message)
    if name in {"SerializationFailure", "DeadlockDetected"} or sqlstate in {
        "40001",
        "40P01",
    }:
        return SerializationFailure(message)
    if name == "UniqueViolation" or sqlstate == "23505":
        return UniqueViolation(message)
    if name == "ForeignKeyViolation" or sqlstate == "23503":
        return ForeignKeyViolation(message)
    if sqlstate.startswith("23"):
        return IntegrityFailure(message)
    return PostgresError(message)


class PostgresAdapter(DatabaseInterface):
    """Default-disabled PostgreSQL adapter with a bounded connection pool."""

    BACKUP_TABLES = (
        "schema_migrations",
        "migration_runs",
        "bot_instances",
        "runtime_config",
        "source_channels",
        "source_identifiers",
        "source_scan_cursors",
        "scan_runs",
        "scan_run_sources",
        "subscriptions",
        "media_objects",
        "media_groups",
        "source_messages",
        "media_group_items",
        "worker_instances",
        "media_jobs",
        "job_items",
        "job_files",
        "job_transfer_state",
        "queue_runtime_state",
        "queue_fairness_state",
        "job_queue_entries",
        "processing_reservations",
        "delivery_intents",
        "delivery_intent_items",
        "target_media_registry",
        "deduplication_keys",
        "dead_media",
        "content_filter_hashes",
        "telegram_requests",
        "runtime_leases",
        "rate_limit_state",
        "runtime_checkpoints",
        "runtime_health",
        "migration_rejects",
        "redis_outbox",
        "job_events",
        "delivery_history",
        "source_scan_events",
        "audit_events",
        "metrics_samples",
    )
    IDENTITY_COLUMNS = (
        ("redis_outbox", "outbox_id"),
        ("source_identifiers", "source_identifier_id"),
        ("job_files", "job_file_id"),
        ("job_transfer_state", "transfer_id"),
        ("job_queue_entries", "queue_entry_id"),
        ("content_filter_hashes", "content_filter_hash_id"),
        ("runtime_checkpoints", "checkpoint_id"),
        ("migration_rejects", "migration_reject_id"),
        ("job_events", "event_id"),
        ("delivery_history", "delivery_history_id"),
        ("source_scan_events", "source_scan_event_id"),
        ("audit_events", "audit_event_id"),
        ("metrics_samples", "metric_sample_id"),
    )

    def __init__(
        self,
        settings: PostgresSettings,
        *,
        pool_factory: Optional[Callable[..., Any]] = None,
        row_factory: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._pool_factory = pool_factory
        self._row_factory = row_factory
        self._sleep = sleep
        self._pool: Any = None
        self._mutex = threading.RLock()
        self._local = threading.local()

    def _load_driver(self) -> tuple[Callable[..., Any], Any]:
        if self._pool_factory is not None:
            return self._pool_factory, self._row_factory
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise ConfigurationError(
                "Book 18 requires the optional psycopg[binary,pool] dependency"
            ) from exc
        return ConnectionPool, dict_row

    def connect(self) -> None:
        if not self.settings.enabled:
            raise ConfigurationError(
                "PostgreSQL is disabled; set ENABLE_POSTGRES_ADAPTER=true only in an approved test environment"
            )
        if not self.settings.database_url:
            raise ConfigurationError("PostgreSQL DATABASE_URL is not configured")
        with self._mutex:
            if self._pool is not None:
                return
            pool_factory, row_factory = self._load_driver()
            kwargs: dict[str, Any] = {
                "conninfo": self.settings.database_url,
                "min_size": self.settings.pool_min,
                "max_size": self.settings.pool_max,
                "timeout": self.settings.pool_timeout_seconds,
                "max_idle": self.settings.pool_max_idle_seconds,
                "max_lifetime": self.settings.pool_max_lifetime_seconds,
                "kwargs": {
                    "autocommit": False,
                    "row_factory": row_factory,
                    "connect_timeout": int(
                        max(1, self.settings.connect_timeout_seconds)
                    ),
                    "sslmode": self.settings.sslmode,
                    "application_name": self.settings.application_name,
                    "options": (
                        f"-c statement_timeout={self.settings.statement_timeout_ms}"
                    ),
                },
                "open": False,
            }
            checker = getattr(pool_factory, "check_connection", None)
            if checker is not None:
                kwargs["check"] = checker
            try:
                pool = pool_factory(**kwargs)
                pool.open(
                    wait=True,
                    timeout=self.settings.pool_timeout_seconds,
                )
                self._pool = pool
            except Exception as exc:
                self._pool = None
                raise _classify_error(exc) from exc

    def _require_pool(self) -> Any:
        if self._pool is None:
            self.connect()
        return self._pool

    def disconnect(self) -> None:
        with self._mutex:
            pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.close(timeout=self.settings.pool_timeout_seconds)
            except TypeError:
                pool.close()
            except Exception as exc:
                raise _classify_error(exc) from exc

    def _transaction_stack(self) -> list[Any]:
        stack = getattr(self._local, "transactions", None)
        if stack is None:
            stack = []
            self._local.transactions = stack
        return stack

    def begin(self) -> None:
        stack = self._transaction_stack()
        try:
            if not stack:
                connection_context = self._require_pool().connection(
                    timeout=self.settings.pool_timeout_seconds
                )
                connection = connection_context.__enter__()
                self._local.connection_context = connection_context
                self._local.connection = connection
            transaction = self._local.connection.transaction()
            transaction.__enter__()
            stack.append(transaction)
        except Exception as exc:
            self._release_thread_connection()
            raise _classify_error(exc) from exc

    def commit(self) -> None:
        stack = self._transaction_stack()
        if not stack:
            raise PostgresError("No active PostgreSQL transaction")
        transaction = stack.pop()
        try:
            transaction.__exit__(None, None, None)
        except Exception as exc:
            raise _classify_error(exc) from exc
        finally:
            if not stack:
                self._release_thread_connection()

    def rollback(self) -> None:
        stack = self._transaction_stack()
        if not stack:
            raise PostgresError("No active PostgreSQL transaction")
        transaction = stack.pop()
        rollback_error = RuntimeError("explicit rollback")
        try:
            transaction.__exit__(
                rollback_error.__class__,
                rollback_error,
                None,
            )
        except Exception:
            # The original operation error must remain primary.
            pass
        finally:
            if not stack:
                self._release_thread_connection()

    def _release_thread_connection(self) -> None:
        connection_context = getattr(self._local, "connection_context", None)
        self._local.connection_context = None
        self._local.connection = None
        self._local.transactions = []
        if connection_context is not None:
            connection_context.__exit__(None, None, None)

    @contextmanager
    def transaction(self):
        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    @contextmanager
    def _connection_scope(self) -> Iterator[Any]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        try:
            with self._require_pool().connection(
                timeout=self.settings.pool_timeout_seconds
            ) as connection:
                yield connection
        except Exception as exc:
            raise _classify_error(exc) from exc

    @staticmethod
    def _is_read_only(statement: str) -> bool:
        normalized = statement.lstrip().upper()
        return normalized.startswith(
            ("SELECT ", "SHOW ", "EXPLAIN ", "VALUES ", "TABLE ")
        )

    def _run_with_retry(
        self,
        operation: Callable[[], Any],
        *,
        retry_allowed: bool,
    ) -> Any:
        last_error: BaseException | None = None
        attempts = self.settings.retry_attempts if retry_allowed else 1
        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except (SerializationFailure, ConnectionFailure, PostgresTimeout) as exc:
                last_error = exc
                if getattr(self._local, "connection", None) is not None:
                    raise
                if attempt >= attempts:
                    break
                delay = min(
                    self.settings.retry_cap_seconds,
                    self.settings.retry_base_seconds * (2 ** (attempt - 1)),
                )
                delay += random.uniform(0.0, min(0.25, delay))
                self._sleep(delay)
        assert last_error is not None
        raise last_error

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> PostgresResult:
        translated = translate_qmark(statement)

        def operation() -> PostgresResult:
            try:
                with self._connection_scope() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(translated, tuple(parameters))
                        return PostgresResult(
                            rowcount=max(-1, int(cursor.rowcount)),
                            status_message=str(
                                getattr(cursor, "statusmessage", "") or ""
                            ),
                        )
            except PostgresError:
                raise
            except Exception as exc:
                raise _classify_error(exc) from exc

        return self._run_with_retry(
            operation,
            retry_allowed=self._is_read_only(translated),
        )

    def executemany(
        self, statement: str, parameter_sets: Sequence[Sequence[Any]]
    ) -> PostgresResult:
        translated = translate_qmark(statement)
        batches = [tuple(values) for values in parameter_sets]

        def operation() -> PostgresResult:
            try:
                with self._connection_scope() as connection:
                    with connection.cursor() as cursor:
                        cursor.executemany(translated, batches)
                        return PostgresResult(
                            rowcount=max(-1, int(cursor.rowcount)),
                            status_message=str(
                                getattr(cursor, "statusmessage", "") or ""
                            ),
                        )
            except PostgresError:
                raise
            except Exception as exc:
                raise _classify_error(exc) from exc

        return self._run_with_retry(operation, retry_allowed=False)

    def fetchone(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        translated = translate_qmark(statement)

        def operation() -> Any:
            try:
                with self._connection_scope() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(translated, tuple(parameters))
                        return cursor.fetchone()
            except PostgresError:
                raise
            except Exception as exc:
                raise _classify_error(exc) from exc

        return self._run_with_retry(operation, retry_allowed=True)

    def fetchall(self, statement: str, parameters: Sequence[Any] = ()) -> list[Any]:
        translated = translate_qmark(statement)

        def operation() -> list[Any]:
            try:
                with self._connection_scope() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(translated, tuple(parameters))
                        return list(cursor.fetchall())
            except PostgresError:
                raise
            except Exception as exc:
                raise _classify_error(exc) from exc

        return self._run_with_retry(operation, retry_allowed=True)

    def insert(self, table: str, values: Mapping[str, Any]) -> int:
        if not values:
            raise IntegrityFailure("Cannot insert an empty mapping")
        columns = list(values)
        statement = (
            f"INSERT INTO {quote_identifier(table)} "
            f"({', '.join(quote_identifier(column) for column in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        return self.execute(statement, [values[column] for column in columns]).rowcount

    def update(
        self,
        table: str,
        values: Mapping[str, Any],
        where: Mapping[str, Any],
    ) -> int:
        if not values or not where:
            raise IntegrityFailure("Update requires values and where")
        value_columns = list(values)
        where_columns = list(where)
        statement = (
            f"UPDATE {quote_identifier(table)} SET "
            + ", ".join(f"{quote_identifier(column)}=?" for column in value_columns)
            + " WHERE "
            + " AND ".join(
                f"{quote_identifier(column)}=?" for column in where_columns
            )
        )
        return self.execute(
            statement,
            [values[column] for column in value_columns]
            + [where[column] for column in where_columns],
        ).rowcount

    def delete(self, table: str, where: Mapping[str, Any]) -> int:
        if not where:
            raise IntegrityFailure("Delete requires where")
        columns = list(where)
        statement = (
            f"DELETE FROM {quote_identifier(table)} WHERE "
            + " AND ".join(f"{quote_identifier(column)}=?" for column in columns)
        )
        return self.execute(statement, [where[column] for column in columns]).rowcount

    def ping(self) -> float:
        started = time.perf_counter()
        row = self.fetchone("SELECT 1 AS ok")
        if row is None:
            raise ConnectionFailure("PostgreSQL ping returned no row")
        return time.perf_counter() - started

    def health(self) -> Mapping[str, Any]:
        ping_latency = self.ping()
        started = time.perf_counter()
        with self.transaction():
            self.fetchone("SELECT 1")
        transaction_latency = time.perf_counter() - started
        status: dict[str, Any] = {
            "ok": True,
            "ping_latency_seconds": ping_latency,
            "transaction_latency_seconds": transaction_latency,
            "pool": {},
        }
        pool = self._require_pool()
        with contextlib.suppress(Exception):
            status["pool"] = dict(pool.get_stats())
        with contextlib.suppress(DatabaseError):
            row = self.fetchone(
                """
                SELECT current_database() AS database,
                       current_user AS user,
                       pg_database_size(current_database()) AS database_bytes
                """
            )
            if row:
                status.update(dict(row))
        with contextlib.suppress(DatabaseError):
            row = self.fetchone(
                """
                SELECT COUNT(*) AS connections
                FROM pg_stat_activity
                WHERE datname=current_database()
                """
            )
            if row:
                status["database_connections"] = int(row["connections"])
        return status

    def execute_script(self, script: str) -> int:
        statements = split_sql_statements(script)
        with self.transaction():
            for statement in statements:
                self.execute(statement)
        return len(statements)

    @staticmethod
    def _qualified_table(table: str) -> str:
        return f'{quote_identifier("royells")}.{quote_identifier(table)}'

    def backup(self, destination: str | os.PathLike[str]) -> None:
        destination_path = Path(destination).expanduser()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        digest = hashlib.sha256()
        counts: dict[str, int] = {}
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination_path.name}.",
                suffix=".tmp",
                dir=str(destination_path.parent),
            )
            os.close(fd)
            temporary_path = Path(temporary_name)
            with gzip.open(temporary_path, "wt", encoding="utf-8", newline="\n") as stream:
                with self.transaction():
                    self.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    header = {
                        "type": "royells_postgres_backup",
                        "format_version": 2,
                        "created_at": datetime.utcnow().isoformat() + "Z",
                        "schema": "royells",
                        "tables": list(self.BACKUP_TABLES),
                    }
                    encoded = json.dumps(
                        header,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ) + "\n"
                    stream.write(encoded)
                    digest.update(encoded.encode("utf-8"))
                    for table in self.BACKUP_TABLES:
                        counts[table] = 0
                        connection = self._local.connection
                        with connection.cursor() as cursor:
                            cursor.execute(
                                f"SELECT * FROM {self._qualified_table(table)}"
                            )
                            while True:
                                rows = cursor.fetchmany(self.settings.batch_size)
                                if not rows:
                                    break
                                for row in rows:
                                    record = {
                                        "type": "row",
                                        "table": table,
                                        "data": dict(row),
                                    }
                                    encoded = json.dumps(
                                        record,
                                        ensure_ascii=True,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                        default=_json_default,
                                    ) + "\n"
                                    stream.write(encoded)
                                    digest.update(encoded.encode("utf-8"))
                                    counts[table] += 1
                    footer = {
                        "type": "footer",
                        "counts": counts,
                        "sha256": digest.hexdigest(),
                    }
                    stream.write(
                        json.dumps(
                            footer,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            os.replace(temporary_path, destination_path)
            temporary_path = None
        except Exception as exc:
            if isinstance(exc, DatabaseError):
                raise
            raise PostgresError("PostgreSQL logical backup failed") from exc
        finally:
            if temporary_path is not None:
                with contextlib.suppress(OSError):
                    temporary_path.unlink()

    def _verify_backup(self, source: Path) -> dict[str, int]:
        digest = hashlib.sha256()
        header: dict[str, Any] | None = None
        footer: dict[str, Any] | None = None
        try:
            with gzip.open(source, "rt", encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line, object_hook=_json_object_hook)
                    if record.get("type") == "royells_postgres_backup":
                        header = record
                    if record.get("type") == "footer":
                        footer = record
                        break
                    digest.update(line.encode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntegrityFailure("PostgreSQL backup archive is invalid") from exc
        if header is None:
            raise IntegrityFailure("PostgreSQL backup header is missing")
        if int(header.get("format_version") or 0) != 2:
            raise IntegrityFailure("PostgreSQL backup format is unsupported")
        if str(header.get("schema") or "") != "royells":
            raise IntegrityFailure("PostgreSQL backup schema is invalid")
        if list(header.get("tables") or []) != list(self.BACKUP_TABLES):
            raise IntegrityFailure("PostgreSQL backup table manifest is incompatible")
        if footer is None:
            raise IntegrityFailure("PostgreSQL backup footer is missing")
        if not hmac.compare_digest(
            str(footer.get("sha256") or ""),
            digest.hexdigest(),
        ):
            raise IntegrityFailure("PostgreSQL backup checksum mismatch")
        counts = {
            str(key): int(value)
            for key, value in footer.get("counts", {}).items()
        }
        if set(counts) != set(self.BACKUP_TABLES):
            raise IntegrityFailure("PostgreSQL backup counts are incomplete")
        return counts

    def _reset_identity_sequences(self) -> None:
        """Advance every restored identity sequence past its maximum value."""

        for table, column in self.IDENTITY_COLUMNS:
            qualified = self._qualified_table(table)
            quoted_column = quote_identifier(column)
            self.fetchone(
                f"""
                SELECT setval(
                    pg_get_serial_sequence(?, ?),
                    COALESCE((SELECT MAX({quoted_column}) FROM {qualified}), 1),
                    EXISTS(SELECT 1 FROM {qualified})
                )
                """,
                (f"royells.{table}", column),
            )

    def restore(self, source: str | os.PathLike[str]) -> None:
        if not self.settings.allow_restore:
            raise ConfigurationError(
                "Logical restore is disabled; set POSTGRES_ALLOW_LOGICAL_RESTORE=true only in an approved recovery environment"
            )
        source_path = Path(source).expanduser()
        if not source_path.is_file():
            raise IntegrityFailure(f"Backup source does not exist: {source_path}")
        expected_counts = self._verify_backup(source_path)
        with self.transaction():
            quoted = ", ".join(
                self._qualified_table(table)
                for table in reversed(self.BACKUP_TABLES)
            )
            self.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
            restored_counts = {table: 0 for table in self.BACKUP_TABLES}
            current_table = ""
            current_columns: list[str] = []
            current_rows: list[dict[str, Any]] = []
            table_order = {table: index for index, table in enumerate(self.BACKUP_TABLES)}
            last_table_index = -1

            def flush_rows() -> None:
                if not current_rows:
                    return
                statement = (
                    f"INSERT INTO {self._qualified_table(current_table)} "
                    f"({', '.join(quote_identifier(column) for column in current_columns)}) "
                    f"VALUES ({', '.join('?' for _ in current_columns)})"
                )
                self.executemany(
                    statement,
                    [
                        [row[column] for column in current_columns]
                        for row in current_rows
                    ],
                )

            with gzip.open(source_path, "rt", encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line, object_hook=_json_object_hook)
                    if record.get("type") != "row":
                        continue
                    table = str(record.get("table") or "")
                    row = record.get("data")
                    if table not in table_order or not isinstance(row, dict):
                        raise IntegrityFailure(
                            "Backup contains an unsafe table or row"
                        )
                    table_index = table_order[table]
                    if table_index < last_table_index:
                        raise IntegrityFailure(
                            "Backup table order is not deterministic"
                        )
                    if table != current_table:
                        flush_rows()
                        current_table = table
                        current_columns = list(row)
                        current_rows = []
                        last_table_index = table_index
                    if list(row) != current_columns:
                        raise IntegrityFailure(
                            f"Backup column mismatch in table {table}"
                        )
                    current_rows.append(row)
                    restored_counts[table] += 1
                    if len(current_rows) >= self.settings.batch_size:
                        flush_rows()
                        current_rows = []
                flush_rows()
            if restored_counts != expected_counts:
                raise IntegrityFailure(
                    "Logical restore row counts do not match the backup manifest"
                )
            self._reset_identity_sequences()

    def close(self) -> None:
        self.disconnect()
