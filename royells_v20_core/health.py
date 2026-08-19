"""Truthful liveness, readiness, startup, and dependency health."""

from __future__ import annotations

import contextlib
import time
from typing import Any, Mapping, Optional

from .configuration import Settings
from .interfaces import (
    AuthorityPolicyInterface,
    CheckpointInterface,
    HealthInterface,
    LifecycleInterface,
    MetricsInterface,
    RecoveryInterface,
)


class LegacyHealthService(HealthInterface):
    """Health service for the compatibility runtime and optional v20 backends."""

    def __init__(
        self,
        bridge: Any,
        settings: Settings,
        authority: AuthorityPolicyInterface,
        checkpoint: CheckpointInterface,
        recovery: RecoveryInterface,
        lifecycle: LifecycleInterface,
        metrics: MetricsInterface,
        *,
        optional_dependencies: Optional[Mapping[str, Any]] = None,
        worker_supervisor: Any = None,
    ) -> None:
        self.bridge = bridge
        self.settings = settings
        self.authority = authority
        self.checkpoint = checkpoint
        self.recovery = recovery
        self.lifecycle = lifecycle
        self.metrics = metrics
        self.optional_dependencies = dict(optional_dependencies or {})
        self.worker_supervisor = worker_supervisor

    @staticmethod
    def _connected(client: Any) -> bool:
        value = getattr(client, "is_connected", None)
        if callable(value):
            with contextlib.suppress(Exception):
                return bool(value())
        return bool(value)

    def liveness(self) -> Mapping[str, Any]:
        return {
            "ok": True,
            "status": "alive",
            "boot_id": str(self.bridge.BOOT_ID),
            "app_version": str(self.bridge.APP_VERSION),
            "uptime_seconds": max(
                0.0,
                time.time() - self.bridge.BOT_START.timestamp(),
            ),
            "shutdown": self.lifecycle.status(),
        }

    def readiness(self) -> Mapping[str, Any]:
        reasons: list[str] = []
        if self.lifecycle.requested():
            reasons.append("shutdown requested")
        bot_connected = self._connected(self.bridge.app)
        userbot_connected = self._connected(self.bridge.userbot)
        if not bot_connected:
            reasons.append("bot client disconnected")
        if not userbot_connected:
            reasons.append("userbot client disconnected")
        if bool(self.bridge.SESSION_AUTH_INVALID):
            reasons.append("Telegram session invalid")
        if bool(self.bridge.DB_RECOVERY_ACTIVE):
            reasons.append("SQLite recovery active")
        if bool(self.bridge.DB_DEGRADED):
            reasons.append("SQLite degraded")
        if not bool(self.bridge.QUEUE_RECOVERY_READY.is_set()):
            reasons.append("queue recovery incomplete")
        return {
            "ok": not reasons,
            "status": "ready" if not reasons else "not_ready",
            "reasons": reasons,
            "telegram": {
                "bot_connected": bot_connected,
                "userbot_connected": userbot_connected,
                "session_invalid": bool(self.bridge.SESSION_AUTH_INVALID),
            },
            "database": {
                "degraded": bool(self.bridge.DB_DEGRADED),
                "recovery_active": bool(self.bridge.DB_RECOVERY_ACTIVE),
                "last_ready_at": float(self.bridge.DB_LAST_READY_AT or 0.0),
            },
            "queue_recovery_ready": bool(
                self.bridge.QUEUE_RECOVERY_READY.is_set()
            ),
        }

    def startup(self) -> Mapping[str, Any]:
        return {
            "ok": bool(self.bridge.QUEUE_RECOVERY_READY.is_set()),
            "status": str(self.bridge.BOOT_STAGE),
            "pipeline": dict(self.bridge.PIPELINE_SCAN_STATUS),
            "checkpoint": dict(self.checkpoint.status()),
            "recovery": dict(self.recovery.status()),
        }

    def snapshot(self) -> Mapping[str, Any]:
        dependencies: dict[str, Any] = {}
        for name, dependency in self.optional_dependencies.items():
            try:
                dependencies[name] = dependency.health()
            except Exception as exc:
                dependencies[name] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
        queues = {
            "download": int(self.bridge.channel_download_queue.qsize()),
            "upload": int(self.bridge.upload_queue.qsize()),
            "link": int(self.bridge.link_process_queue.qsize()),
            "button": int(self.bridge.button_queue.qsize()),
            "retry": int(self.bridge.retry_admission_queue.qsize()),
        }
        return {
            "ok": bool(self.readiness()["ok"]),
            "service": "royells-mirror-bot",
            "architecture": "v20-compatibility",
            "liveness": self.liveness(),
            "readiness": self.readiness(),
            "startup": self.startup(),
            "authority": dict(self.authority.snapshot()),
            "configuration": self.settings.redacted(),
            "queues": queues,
            "metrics": dict(self.metrics.snapshot()),
            "workers": (
                self.worker_supervisor.snapshot()
                if self.worker_supervisor is not None
                else {}
            ),
            "optional_dependencies": dependencies,
            "time": time.time(),
        }
