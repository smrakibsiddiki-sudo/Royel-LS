"""Injected worker wrappers and task supervision."""

from __future__ import annotations

import asyncio
import contextlib
import time
from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping

from .errors import WorkerError
from .models import WorkerState
from .worker_contracts import WorkerDependencies, WorkerInterface


WorkerRunner = Callable[[], Awaitable[None]]


class FunctionWorker(WorkerInterface):
    """Adapt one existing worker coroutine to the v20 lifecycle contract."""

    def __init__(
        self,
        dependencies: WorkerDependencies,
        *,
        worker_id: str,
        worker_type: str,
        runner: WorkerRunner,
        restart_on_return: bool = True,
        restart_on_error: bool = True,
        restart_delay_seconds: float = 5.0,
    ) -> None:
        super().__init__(dependencies)
        self.worker_id = str(worker_id)
        self.worker_type = str(worker_type)
        self.runner = runner
        self.restart_on_return = bool(restart_on_return)
        self.restart_on_error = bool(restart_on_error)
        self.restart_delay_seconds = max(0.0, float(restart_delay_seconds))

    async def run(self) -> None:
        await self.runner()


class DownloaderWorker(FunctionWorker):
    """Injected compatibility wrapper for one downloader loop."""


class UploaderWorker(FunctionWorker):
    """Injected compatibility wrapper for one uploader loop."""


class RetryWorker(FunctionWorker):
    """Injected compatibility wrapper for retry scheduling."""


class AlbumWorker(FunctionWorker):
    """Injected compatibility wrapper for album assembly."""


class ButtonWorker(FunctionWorker):
    """Injected compatibility wrapper for control-plane callbacks."""


class LinkWorker(FunctionWorker):
    """Injected compatibility wrapper for Telegram link processing."""


class CleanupWorker(FunctionWorker):
    """Injected compatibility wrapper for cleanup and queue healing."""


class DatabaseWriterWorker(FunctionWorker):
    """Injected compatibility wrapper for serialized database writers."""


class MonitoringWorker(FunctionWorker):
    """Injected compatibility wrapper for monitoring and maintenance loops."""


class TaskSupervisor:
    """Own every long-running task and observe failures uniformly."""

    def __init__(self, dependencies: WorkerDependencies) -> None:
        self.dependencies = dependencies
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._workers: dict[str, WorkerInterface] = {}
        self._states: dict[str, WorkerState] = {}
        self._lock = asyncio.Lock()
        self._stopping = False

    def _state(
        self,
        worker: WorkerInterface,
        *,
        status: str,
        started_at: float,
        restart_count: int,
        last_error: str = "",
    ) -> WorkerState:
        return WorkerState(
            worker_id=worker.worker_id,
            worker_type=worker.worker_type,
            status=status,
            heartbeat_at=time.time(),
            started_at=started_at,
            restart_count=restart_count,
            last_error=last_error[:1000],
        )

    async def _run(self, worker: WorkerInterface) -> None:
        started_at = time.time()
        restart_count = 0
        logger = self.dependencies.logger
        metrics = self.dependencies.metrics
        while not self._stopping and not self.dependencies.lifecycle.requested():
            self._states[worker.worker_id] = self._state(
                worker,
                status="running",
                started_at=started_at,
                restart_count=restart_count,
            )
            logger.info(
                "Task start",
                worker_id=worker.worker_id,
                worker_type=worker.worker_type,
                restart_count=restart_count,
            )
            run_started = time.monotonic()
            try:
                await worker.run()
                duration = time.monotonic() - run_started
                metrics.observe(
                    "worker_run_seconds",
                    duration,
                    worker_type=worker.worker_type,
                )
                if not worker.restart_on_return or self._stopping:
                    self._states[worker.worker_id] = self._state(
                        worker,
                        status="completed",
                        started_at=started_at,
                        restart_count=restart_count,
                    )
                    return
                logger.warn(
                    "Worker returned; restarting loop",
                    worker_id=worker.worker_id,
                    duration_seconds=duration,
                )
            except asyncio.CancelledError:
                self._states[worker.worker_id] = self._state(
                    worker,
                    status="cancelled",
                    started_at=started_at,
                    restart_count=restart_count,
                )
                raise
            except Exception as exc:
                duration = time.monotonic() - run_started
                metrics.increment(
                    "worker_errors_total", worker_type=worker.worker_type
                )
                self._states[worker.worker_id] = self._state(
                    worker,
                    status="error",
                    started_at=started_at,
                    restart_count=restart_count,
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                logger.error(
                    "Worker failed",
                    worker_id=worker.worker_id,
                    worker_type=worker.worker_type,
                    duration_seconds=duration,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if not worker.restart_on_error or self._stopping:
                    return
            restart_count += 1
            self._states[worker.worker_id] = self._state(
                worker,
                status="restart_wait",
                started_at=started_at,
                restart_count=restart_count,
            )
            await asyncio.sleep(worker.restart_delay_seconds)

    def spawn(self, worker: WorkerInterface) -> asyncio.Task[Any]:
        if self._stopping:
            raise WorkerError("Task supervisor is stopping")
        if worker.worker_id in self._tasks:
            raise WorkerError(f"Duplicate worker id: {worker.worker_id}")
        task = asyncio.create_task(
            self._run(worker),
            name=worker.worker_id,
        )
        self._workers[worker.worker_id] = worker
        self._tasks[worker.worker_id] = task

        def observe(completed: asyncio.Task[Any]) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                error = completed.exception()
                if error is not None:
                    self.dependencies.logger.error(
                        "Supervised task terminated",
                        worker_id=worker.worker_id,
                        error=f"{type(error).__name__}: {error}",
                    )

        task.add_done_callback(observe)
        return task

    async def stop(self, timeout_seconds: float = 30.0) -> None:
        self._stopping = True
        workers = list(self._workers.values())
        for worker in workers:
            with contextlib.suppress(Exception):
                await worker.drain()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(0.1, float(timeout_seconds)),
                )
        self._tasks.clear()
        self._workers.clear()

    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(self._tasks.values())

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "stopping": self._stopping,
            "workers": {
                worker_id: deepcopy(state.__dict__)
                for worker_id, state in self._states.items()
            },
            "task_count": len(self._tasks),
            "running_count": sum(
                1 for task in self._tasks.values() if not task.done()
            ),
        }
