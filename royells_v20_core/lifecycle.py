"""Graceful application lifecycle coordination."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Mapping

from .interfaces import LifecycleInterface


class ApplicationLifecycle(LifecycleInterface):
    """Thread-safe shutdown request consumed by the main asyncio loop."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requested = False
        self._reason = ""
        self._exit_code = 0
        self._requested_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def request_shutdown(self, reason: str, exit_code: int = 0) -> None:
        with self._lock:
            if not self._requested:
                self._requested = True
                self._reason = str(reason or "shutdown requested")[:500]
                self._exit_code = int(exit_code)
                self._requested_at = time.time()
            loop = self._loop
            event = self._event
        if loop is not None and event is not None and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

    async def wait(self) -> tuple[str, int]:
        loop = asyncio.get_running_loop()
        with self._lock:
            self._loop = loop
            if self._event is None:
                self._event = asyncio.Event()
            event = self._event
            already_requested = self._requested
        if already_requested:
            event.set()
        await event.wait()
        with self._lock:
            return self._reason, self._exit_code

    def requested(self) -> bool:
        with self._lock:
            return self._requested

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "shutdown_requested": self._requested,
                "reason": self._reason,
                "exit_code": self._exit_code,
                "requested_at": self._requested_at,
            }
