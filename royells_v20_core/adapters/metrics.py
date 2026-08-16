"""Thread-safe in-memory MetricsInterface implementation."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

from ..interfaces import MetricsInterface


def _metric_key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return str(name)
    suffix = ",".join(f"{key}={labels[key]}" for key in sorted(labels))
    return f"{name}{{{suffix}}}"


class InMemoryMetrics(MetricsInterface):
    """Bounded-by-metric-name metrics suitable for legacy compatibility."""

    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._observations: dict[str, dict[str, float]] = {}
        self._lock = threading.RLock()

    def increment(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        key = _metric_key(name, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def decrement(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        self.increment(name, -float(amount), **labels)

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = _metric_key(name, labels)
        numeric = float(value)
        with self._lock:
            current = self._observations.setdefault(
                key,
                {"count": 0.0, "sum": 0.0, "min": numeric, "max": numeric},
            )
            current["count"] += 1.0
            current["sum"] += numeric
            current["min"] = min(current["min"], numeric)
            current["max"] = max(current["max"], numeric)

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        key = _metric_key(name, labels)
        with self._lock:
            self._values[key] = float(value)

    @contextmanager
    def timer(self, name: str, **labels: Any):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - started, **labels)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "values": deepcopy(self._values),
                "observations": deepcopy(self._observations),
            }
