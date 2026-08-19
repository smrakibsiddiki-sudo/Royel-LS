"""Configurable PostgreSQL batch writer."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Mapping, Sequence

from royells_v20_core.interfaces import DatabaseInterface

from .sql import quote_identifier


class PostgresBatchWriter:
    """Bounded in-memory batch assembler using one executemany call per flush."""

    def __init__(
        self,
        database: DatabaseInterface,
        table: str,
        columns: Sequence[str],
        *,
        batch_size: int = 500,
        max_pending: int = 10000,
    ) -> None:
        self.database = database
        self.table = quote_identifier(table)
        self.columns = tuple(columns)
        if not self.columns:
            raise ValueError("Batch writer requires columns")
        self.quoted_columns = tuple(quote_identifier(column) for column in columns)
        self.batch_size = max(1, int(batch_size))
        self.max_pending = max(self.batch_size, int(max_pending))
        self._pending: deque[tuple[Any, ...]] = deque()
        self._lock = threading.RLock()

    def add(self, row: Mapping[str, Any]) -> int:
        values = tuple(row[column] for column in self.columns)
        with self._lock:
            if len(self._pending) >= self.max_pending:
                raise BufferError("PostgreSQL batch writer is full")
            self._pending.append(values)
            return len(self._pending)

    def flush(self) -> int:
        with self._lock:
            count = min(self.batch_size, len(self._pending))
            rows = [self._pending.popleft() for _ in range(count)]
        if not rows:
            return 0
        statement = (
            f"INSERT INTO {self.table} "
            f"({', '.join(self.quoted_columns)}) "
            f"VALUES ({', '.join('?' for _ in self.columns)})"
        )
        try:
            with self.database.transaction():
                self.database.executemany(statement, rows)
        except BaseException:
            with self._lock:
                self._pending.extendleft(reversed(rows))
            raise
        return len(rows)

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)
