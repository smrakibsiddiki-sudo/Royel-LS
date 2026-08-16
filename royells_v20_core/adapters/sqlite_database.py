"""SQLite implementation of DatabaseInterface.

This adapter owns every sqlite3 import used by Book 17.  It is not connected
to the v19 production entry point yet, so existing DB recovery and schema code
remain authoritative until a later migration book explicitly wires it in.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import DatabaseError
from ..interfaces import DatabaseInterface


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(value: str) -> str:
    """Validate a SQL identifier before interpolation."""

    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise DatabaseError(f"Unsafe SQL identifier: {value!r}")
    return f'"{value}"'


class SQLiteAdapter(DatabaseInterface):
    """Thread-safe, explicit-transaction SQLite adapter."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float = 30.0,
        journal_mode: str = "DELETE",
        synchronous: str = "NORMAL",
    ) -> None:
        self.path = Path(path).expanduser()
        self.timeout = max(0.1, float(timeout))
        self.journal_mode = str(journal_mode or "DELETE").upper()
        self.synchronous = str(synchronous or "NORMAL").upper()
        if self.journal_mode not in {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}:
            raise DatabaseError(f"Unsupported SQLite journal mode: {self.journal_mode}")
        if self.synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise DatabaseError(f"Unsupported SQLite synchronous mode: {self.synchronous}")
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._local = threading.local()

    def connect(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    str(self.path),
                    timeout=self.timeout,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                connection.execute(
                    f"PRAGMA busy_timeout={int(self.timeout * 1000)}"
                )
                connection.execute(f"PRAGMA journal_mode={self.journal_mode}")
                connection.execute(f"PRAGMA synchronous={self.synchronous}")
                self._connection = connection
            except (sqlite3.Error, OSError) as exc:
                raise DatabaseError(f"SQLite connect failed: {self.path}") from exc

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.connect()
        assert self._connection is not None
        return self._connection

    def disconnect(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                with contextlib.suppress(sqlite3.Error):
                    connection.close()

    def begin(self) -> None:
        depth = int(getattr(self._local, "transaction_depth", 0))
        if depth == 0:
            self._lock.acquire()
        try:
            connection = self._require_connection()
            if depth == 0:
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute(f"SAVEPOINT royells_v20_{depth}")
            self._local.transaction_depth = depth + 1
        except (sqlite3.Error, OSError, DatabaseError) as exc:
            if depth == 0:
                self._lock.release()
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError("SQLite begin failed") from exc

    def commit(self) -> None:
        depth = int(getattr(self._local, "transaction_depth", 0))
        if depth <= 0:
            # sqlite3 starts implicit transactions for legacy callers that
            # execute DML directly. Preserve that behavior while keeping
            # explicit nested transactions protected by the adapter lock.
            with self._lock:
                try:
                    self._require_connection().commit()
                except sqlite3.Error as exc:
                    raise DatabaseError("SQLite commit failed") from exc
            return
        next_depth = depth - 1
        try:
            connection = self._require_connection()
            if next_depth == 0:
                connection.commit()
            else:
                connection.execute(f"RELEASE SAVEPOINT royells_v20_{next_depth}")
            self._local.transaction_depth = next_depth
        except sqlite3.Error as exc:
            raise DatabaseError("SQLite commit failed") from exc
        finally:
            if next_depth == 0:
                self._lock.release()

    def rollback(self) -> None:
        depth = int(getattr(self._local, "transaction_depth", 0))
        if depth <= 0:
            # Match sqlite3.Connection.rollback() for legacy callers when no
            # adapter-managed transaction is active.
            with self._lock:
                try:
                    self._require_connection().rollback()
                except sqlite3.Error as exc:
                    raise DatabaseError("SQLite rollback failed") from exc
            return
        next_depth = depth - 1
        try:
            connection = self._require_connection()
            if next_depth == 0:
                connection.rollback()
            else:
                savepoint = f"royells_v20_{next_depth}"
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            self._local.transaction_depth = next_depth
        except sqlite3.Error as exc:
            raise DatabaseError("SQLite rollback failed") from exc
        finally:
            if next_depth == 0:
                self._lock.release()

    @contextmanager
    def transaction(self):
        """Commit on success and roll back on failure."""

        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        with self._lock:
            try:
                return self._require_connection().execute(statement, tuple(parameters))
            except sqlite3.Error as exc:
                raise DatabaseError("SQLite execute failed") from exc

    def executemany(
        self, statement: str, parameter_sets: Sequence[Sequence[Any]]
    ) -> Any:
        with self._lock:
            try:
                return self._require_connection().executemany(
                    statement, [tuple(values) for values in parameter_sets]
                )
            except sqlite3.Error as exc:
                raise DatabaseError("SQLite executemany failed") from exc

    def fetchone(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        with self._lock:
            try:
                return self._require_connection().execute(
                    statement, tuple(parameters)
                ).fetchone()
            except sqlite3.Error as exc:
                raise DatabaseError("SQLite fetchone failed") from exc

    def fetchall(self, statement: str, parameters: Sequence[Any] = ()) -> list[Any]:
        with self._lock:
            try:
                return list(
                    self._require_connection()
                    .execute(statement, tuple(parameters))
                    .fetchall()
                )
            except sqlite3.Error as exc:
                raise DatabaseError("SQLite fetchall failed") from exc

    def insert(self, table: str, values: Mapping[str, Any]) -> int:
        if not values:
            raise DatabaseError("Cannot insert an empty mapping")
        columns = list(values)
        statement = (
            f"INSERT INTO {_identifier(table)} "
            f"({', '.join(_identifier(column) for column in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        cursor = self.execute(statement, [values[column] for column in columns])
        return int(cursor.rowcount if cursor.rowcount >= 0 else 0)

    def update(
        self,
        table: str,
        values: Mapping[str, Any],
        where: Mapping[str, Any],
    ) -> int:
        if not values or not where:
            raise DatabaseError("Update requires values and a where mapping")
        value_columns = list(values)
        where_columns = list(where)
        statement = (
            f"UPDATE {_identifier(table)} SET "
            + ", ".join(f"{_identifier(column)}=?" for column in value_columns)
            + " WHERE "
            + " AND ".join(f"{_identifier(column)}=?" for column in where_columns)
        )
        cursor = self.execute(
            statement,
            [values[column] for column in value_columns]
            + [where[column] for column in where_columns],
        )
        return int(cursor.rowcount if cursor.rowcount >= 0 else 0)

    def delete(self, table: str, where: Mapping[str, Any]) -> int:
        if not where:
            raise DatabaseError("Delete requires a where mapping")
        columns = list(where)
        statement = (
            f"DELETE FROM {_identifier(table)} WHERE "
            + " AND ".join(f"{_identifier(column)}=?" for column in columns)
        )
        cursor = self.execute(statement, [where[column] for column in columns])
        return int(cursor.rowcount if cursor.rowcount >= 0 else 0)

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            try:
                connection = self._require_connection()
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                return {
                    "ok": str(result).lower() == "ok",
                    "integrity": str(result),
                    "path": str(self.path),
                }
            except sqlite3.Error as exc:
                raise DatabaseError("SQLite health check failed") from exc

    def ping(self) -> float:
        started = time.perf_counter()
        self.fetchone("SELECT 1")
        return time.perf_counter() - started

    def backup(self, destination: str | os.PathLike[str]) -> None:
        destination_path = Path(destination).expanduser()
        temporary_path: Path | None = None
        with self._lock:
            try:
                connection = self._require_connection()
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination_path.name}.",
                    suffix=".tmp",
                    dir=str(destination_path.parent),
                )
                os.close(fd)
                temporary_path = Path(temporary_name)
                target = sqlite3.connect(str(temporary_path), timeout=self.timeout)
                try:
                    connection.backup(target)
                    target.commit()
                finally:
                    target.close()
                os.replace(temporary_path, destination_path)
                temporary_path = None
            except (sqlite3.Error, OSError) as exc:
                raise DatabaseError("SQLite backup failed") from exc
            finally:
                if temporary_path is not None:
                    with contextlib.suppress(OSError):
                        temporary_path.unlink()

    def restore(self, source: str | os.PathLike[str]) -> None:
        source_path = Path(source).expanduser()
        if not source_path.is_file():
            raise DatabaseError(f"SQLite restore source does not exist: {source_path}")
        temporary_path: Path | None = None
        with self._lock:
            try:
                source_db = sqlite3.connect(str(source_path), timeout=self.timeout)
                try:
                    integrity = source_db.execute("PRAGMA integrity_check").fetchone()[0]
                    if str(integrity).lower() != "ok":
                        raise DatabaseError(
                            f"SQLite restore source failed integrity check: {integrity}"
                        )
                finally:
                    source_db.close()
                self.disconnect()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary_name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.",
                    suffix=".restore",
                    dir=str(self.path.parent),
                )
                os.close(fd)
                temporary_path = Path(temporary_name)
                shutil.copy2(source_path, temporary_path)
                os.replace(temporary_path, self.path)
                temporary_path = None
                self.connect()
            except DatabaseError:
                raise
            except (sqlite3.Error, OSError) as exc:
                raise DatabaseError("SQLite restore failed") from exc
            finally:
                if temporary_path is not None:
                    with contextlib.suppress(OSError):
                        temporary_path.unlink()

    def close(self) -> None:
        """Alias for disconnect required by DatabaseInterface."""

        self.disconnect()
