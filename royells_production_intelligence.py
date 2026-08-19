"""Health and incident manifests for Royells production intelligence."""

from __future__ import annotations

import json
import os
import platform
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


HEALTH_MANIFEST_FILENAME = "health_manifest.json"
INCIDENT_MANIFEST_FILENAME = "incident_manifest.json"
INTELLIGENCE_MANIFEST_VERSION = 2
GENERATED_BY = "royells-production-intelligence-v2"
DISK_CAPACITY_CEILING_ENV = "ROYELLS_DISK_CAPACITY_CEILING_BYTES"
DEFAULT_DISK_CAPACITY_CEILING_BYTES = 8 * 1024**5
MEBIBYTE = 1024 * 1024

INCIDENT_PATTERNS = (
    ("FloodWait", "warning", "telegram", ("floodwait", "flood_wait", "420 flood")),
    ("MEDIA_EMPTY", "error", "upload", ("media_empty", "media empty")),
    ("FILE_PART_X_MISSING", "error", "upload", ("file_part_", "file_part_x_missing")),
    ("CHAT_FORWARDS_RESTRICTED", "warning", "telegram", ("chat_forwards_restricted", "forwards restricted")),
    ("AUTH_KEY_DUPLICATED", "critical", "telegram", ("auth_key_duplicated", "authkeyduplicated")),
    ("SESSION_REVOKED", "critical", "telegram", ("session_revoked", "session revoked", "auth key unregistered")),
    ("Timeout", "warning", "network", ("timeout", "timed out")),
    ("Worker Stall", "error", "worker", ("stalled worker", "worker job stalled", "cancelled stalled worker")),
    ("Queue Overflow", "error", "queue", ("queue full", "queue overflow", "admission full")),
    ("Database Busy", "warning", "database", ("database is locked", "sqlite busy", "database busy")),
    ("SQLite Corruption", "critical", "database", ("database disk image is malformed", "sqlite corruption", "malformed database")),
    ("Checkpoint Failure", "error", "checkpoint", ("checkpoint failed", "checkpoint error")),
    ("Recovery Failure", "error", "recovery", ("recovery failed", "recovery error")),
    ("Download Failure", "error", "download", ("download failed", "download error", "hybrid download failed")),
    ("Upload Failure", "error", "upload", ("upload failed", "up error", "upload error")),
    ("Retry Exhausted", "error", "retry", ("retry exhausted", "max retries")),
    ("Duplicate Writer", "warning", "duplicate", ("duplicate writer", "duplicate write")),
    ("Zero-byte Media", "error", "download", ("0-byte media", "zero-byte media", "0 byte media")),
    ("Dead Media", "warning", "media", ("dead-media", "dead media", "skipped_dead_media")),
    ("FFmpeg Failure", "error", "media", ("ffmpeg conversion failed", "ffmpeg failed")),
    ("Unhandled Exception", "critical", "runtime", ("traceback", "unhandled exception", "fatal crash")),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def sane_disk_snapshot(
    runtime_dir: str | os.PathLike[str],
    *,
    capacity_ceiling_bytes: int | None = None,
) -> dict[str, Any]:
    """Return trusted disk metrics while preserving untrusted provider values.

    Some virtual filesystems expose a synthetic multi-petabyte capacity even
    though the container has a much smaller quota. Publishing those values as
    real capacity hides disk pressure, so trusted compatibility fields are
    zeroed whenever the snapshot is unavailable or outside the configured
    sanity boundary. Raw provider values remain available for diagnostics.

    Args:
        runtime_dir: Filesystem path whose backing volume should be measured.
        capacity_ceiling_bytes: Optional positive upper bound. When omitted,
            ``ROYELLS_DISK_CAPACITY_CEILING_BYTES`` is used, falling back to
            eight PiB.

    Returns:
        A JSON-safe mapping containing trusted compatibility fields, status,
        anomaly details, the applied ceiling, and the original raw values.
    """

    configured_ceiling = capacity_ceiling_bytes
    if configured_ceiling is None:
        configured_ceiling = safe_int(
            os.environ.get(DISK_CAPACITY_CEILING_ENV),
            DEFAULT_DISK_CAPACITY_CEILING_BYTES,
        )
    ceiling = safe_int(configured_ceiling, DEFAULT_DISK_CAPACITY_CEILING_BYTES)
    if ceiling <= 0:
        ceiling = DEFAULT_DISK_CAPACITY_CEILING_BYTES

    payload: dict[str, Any] = {
        "available": False,
        "trusted": False,
        "status": "unavailable",
        "anomaly": "",
        "capacity_ceiling_bytes": ceiling,
        "total": 0,
        "used": 0,
        "free": 0,
        "free_mb": 0,
        "raw": {"total": 0, "used": 0, "free": 0, "free_mb": 0},
    }

    try:
        disk = shutil.disk_usage(str(Path(runtime_dir)))
        raw_total = int(disk.total)
        raw_used = int(disk.used)
        raw_free = int(disk.free)
        payload["raw"] = {
            "total": raw_total,
            "used": raw_used,
            "free": raw_free,
            "free_mb": max(0, raw_free // MEBIBYTE),
        }

        if raw_total > ceiling:
            payload.update(status="untrusted", anomaly="capacity_exceeds_ceiling")
            return payload
        if raw_total <= 0 or raw_used < 0 or raw_free < 0 or raw_used > raw_total or raw_free > raw_total:
            payload.update(status="untrusted", anomaly="inconsistent_disk_usage")
            return payload

        payload.update(
            available=True,
            trusted=True,
            status="ok",
            total=raw_total,
            used=raw_used,
            free=raw_free,
            free_mb=raw_free // MEBIBYTE,
        )
        return payload
    except Exception as exc:
        payload["anomaly"] = f"disk_usage_unavailable:{type(exc).__name__}"
        return payload


def process_resources(runtime_dir: str | os.PathLike[str]) -> dict[str, Any]:
    runtime_path = Path(runtime_dir)
    memory_mb = 0.0
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = float(getattr(usage, "ru_maxrss", 0.0))
        memory_mb = rss / 1024.0
        if platform.system().lower() == "darwin":
            memory_mb = rss / (1024.0 * 1024.0)
    except Exception:
        memory_mb = 0.0
    disk_payload = sane_disk_snapshot(runtime_path)
    open_files = 0
    fd_dir = Path("/proc/self/fd")
    if fd_dir.is_dir():
        try:
            open_files = len(list(fd_dir.iterdir()))
        except Exception:
            open_files = 0
    try:
        load_average = list(os.getloadavg())
    except Exception:
        load_average = []
    cpu_times = os.times()
    return {
        "memory_mb": round(memory_mb, 2),
        "cpu": {
            "process_user_seconds": safe_float(cpu_times.user),
            "process_system_seconds": safe_float(cpu_times.system),
            "children_user_seconds": safe_float(cpu_times.children_user),
            "children_system_seconds": safe_float(cpu_times.children_system),
            "load_average": load_average,
        },
        "disk": disk_payload,
        "threads": threading.active_count(),
        "open_files": open_files,
        "platform": {
            "python": platform.python_version(),
            "system": platform.system().lower(),
            "architecture": platform.machine(),
        },
    }


def classify_incident(incident_type: str, exception_text: str = "", raw_text: str = "") -> str:
    """Classify an incident without turning retryable media states permanent.

    Missing or incomplete bytes can be caused by stale Telegram references,
    interrupted transfers, or deferred work and therefore remain retryable.
    Only explicit, confirmed container corruption is terminal media evidence.
    """

    text = f"{incident_type} {exception_text} {raw_text}".lower()
    confirmed_corruption_markers = (
        "confirmed corrupt",
        "confirmed corrupted",
        "confirmed_corrupt",
        "corruption confirmed",
        "corrupt_container",
        "corrupt container",
        "container is corrupt",
        "irrecoverably corrupt",
    )
    if any(marker in text for marker in confirmed_corruption_markers):
        return "Permanent"
    retryable_media_markers = (
        "0-byte",
        "zero-byte",
        "0 byte",
        "media_empty",
        "media empty",
        "incomplete",
        "partial download",
        "deferred",
        "retryable",
        "retrying",
        "retry scheduled",
    )
    if any(marker in text for marker in retryable_media_markers):
        return "Temporary"
    if any(marker in text for marker in ("flood", "timeout", "timed out", "temporar", "network", "connection reset")):
        return "Temporary"
    if any(marker in text for marker in ("telegram", "auth_key", "session", "chat_forwards")):
        return "Telegram"
    if any(marker in text for marker in ("sqlite", "database", "db ")):
        return "Database"
    if any(marker in text for marker in ("file", "directory", "disk", "storage", "permission")):
        return "Filesystem"
    if any(marker in text for marker in ("configuration", "environment", "manifest")):
        return "Configuration"
    if any(marker in text for marker in ("assert", "keyerror", "typeerror", "valueerror")):
        return "Logic"
    return "Unknown"


def incident_classification_details(
    incident_type: str,
    subsystem: str,
    exception_text: str = "",
    raw_text: str = "",
) -> dict[str, str]:
    """Return typed diagnostics without inferring permanence from a symptom label."""

    text = f"{incident_type} {subsystem} {exception_text} {raw_text}".lower()
    root_cause = classify_incident(incident_type, exception_text, raw_text)
    if any(marker in text for marker in ("media", "0-byte", "zero-byte", "container")):
        fault_domain = "media_integrity"
    elif any(marker in text for marker in ("flood", "timeout", "network", "connection")):
        fault_domain = "telegram_transport"
    elif any(marker in text for marker in ("sqlite", "database", "db ")):
        fault_domain = "database"
    elif any(marker in text for marker in ("disk", "storage", "file", "directory")):
        fault_domain = "filesystem"
    else:
        fault_domain = str(subsystem or "runtime")
    retryability = {
        "Temporary": "retryable",
        "Permanent": "non_retryable",
    }.get(root_cause, "indeterminate")
    confidence = "high" if root_cause in {"Temporary", "Permanent"} else "medium"
    return {
        "root_cause": root_cause,
        "fault_domain": fault_domain,
        "retryability": retryability,
        "confidence": confidence,
    }


def incident_type_from_text(text: str) -> tuple[str, str, str] | None:
    lowered = str(text or "").lower()
    for incident_type, severity, subsystem, markers in INCIDENT_PATTERNS:
        if any(marker in lowered for marker in markers):
            return incident_type, severity, subsystem
    return None


class ProductionIntelligenceSystem:
    """Own writable health and incident manifests."""

    def __init__(
        self,
        runtime_dir: str | os.PathLike[str],
        *,
        logger: Callable[[str], Any] | None = None,
        incident_max_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.health_path = self.runtime_dir / HEALTH_MANIFEST_FILENAME
        self.incident_path = self.runtime_dir / INCIDENT_MANIFEST_FILENAME
        self.incident_max_bytes = max(256 * 1024, int(incident_max_bytes))
        self.logger = logger or (lambda _message: None)
        self._lock = threading.RLock()
        self._incident_rate: dict[str, float] = {}

    def ensure(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if not self.health_path.exists():
            self.write_health_manifest(
                {
                    "manifest_version": INTELLIGENCE_MANIFEST_VERSION,
                    "generated_by": GENERATED_BY,
                    "generated_at": utc_now_iso(),
                    "status": "created",
                }
            )
        if not self.incident_path.exists():
            self.incident_path.write_text("", encoding="utf-8", newline="\n")

    def write_health_manifest(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        document = dict(payload or {})
        document.setdefault("manifest_version", INTELLIGENCE_MANIFEST_VERSION)
        document.setdefault("generated_by", GENERATED_BY)
        document["refreshed_at"] = utc_now_iso()
        atomic_write_json(self.health_path, document)
        return document

    def rotate_incidents_if_needed(self) -> None:
        if not self.incident_path.exists():
            return
        try:
            if self.incident_path.stat().st_size < self.incident_max_bytes:
                return
            rotated = self.runtime_dir / f"incident_manifest.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json"
            self.incident_path.replace(rotated)
            self.incident_path.write_text("", encoding="utf-8", newline="\n")
        except Exception as exc:
            self.logger(f"Incident manifest rotation skipped: {type(exc).__name__}: {exc}")

    def should_record(self, key: str, minimum_seconds: float = 30.0) -> bool:
        now = time.monotonic()
        with self._lock:
            previous = float(self._incident_rate.get(key) or 0.0)
            if now - previous < minimum_seconds:
                return False
            self._incident_rate[key] = now
            return True

    def record_incident(
        self,
        *,
        incident_type: str,
        severity: str,
        subsystem: str,
        context: Mapping[str, Any] | None = None,
        exception: BaseException | None = None,
        stack_trace: str = "",
        recovery: Mapping[str, Any] | None = None,
        snapshots: Mapping[str, Any] | None = None,
        rate_limit_key: str = "",
        raw_text: str = "",
    ) -> dict[str, Any]:
        self.ensure()
        exception_text = ""
        exception_class = ""
        if exception is not None:
            exception_class = exception.__class__.__name__
            exception_text = str(exception)
            if not stack_trace:
                stack_trace = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        key = rate_limit_key or f"{incident_type}:{subsystem}:{exception_class}:{exception_text[:120]}"
        if not self.should_record(key):
            return {}
        payload_context = dict(context or {})
        resource_snapshot = process_resources(self.runtime_dir)
        classification = incident_classification_details(
            incident_type,
            subsystem,
            exception_text,
            raw_text,
        )
        incident = {
            "manifest_version": INTELLIGENCE_MANIFEST_VERSION,
            "incident_uuid": uuid.uuid4().hex,
            "timestamp": utc_now_iso(),
            "severity": str(severity or "warning"),
            "incident_type": str(incident_type or "Unhandled Exception"),
            "subsystem": str(subsystem or "runtime"),
            "worker": payload_context.get("worker", ""),
            "queue": payload_context.get("queue", ""),
            "source": payload_context.get("source", ""),
            "target": payload_context.get("target", ""),
            "job_id": payload_context.get("job_id", ""),
            "media_uid": payload_context.get("media_uid", ""),
            "album_id": payload_context.get("album_id", ""),
            "message_id": payload_context.get("message_id", ""),
            "chat_id": payload_context.get("chat_id", ""),
            "exception": {
                "class": exception_class,
                "message": exception_text[:2000],
            },
            "stack_trace": str(stack_trace or "")[-8000:],
            "telegram_method": payload_context.get("telegram_method", ""),
            "telegram_error": payload_context.get("telegram_error", ""),
            "retry_count": safe_int(payload_context.get("retry_count"), 0),
            "worker_runtime": safe_float(payload_context.get("worker_runtime"), 0.0),
            "memory": resource_snapshot.get("memory_mb", 0.0),
            "cpu": resource_snapshot.get("cpu", {}),
            "disk": resource_snapshot.get("disk", {}),
            "queue_sizes": payload_context.get("queue_sizes", {}),
            "checkpoint_state": payload_context.get("checkpoint_state", {}),
            "recovery_state": payload_context.get("recovery_state", {}),
            "database_state": payload_context.get("database_state", {}),
            "manifest_version_text": payload_context.get("manifest_version", INTELLIGENCE_MANIFEST_VERSION),
            "root_cause": classification["root_cause"],
            "fault_domain": classification["fault_domain"],
            "retryability": classification["retryability"],
            "classification_confidence": classification["confidence"],
            "build_identity": dict(payload_context.get("build_identity") or {}),
            "recovery": {
                "attempted": False,
                "successful": False,
                "failed": False,
                "fallback_used": False,
                "retry_scheduled": False,
                "dead_media": False,
                "circuit_breaker_activated": False,
                **dict(recovery or {}),
            },
            "diagnostic_snapshots": dict(snapshots or {}),
        }
        line = json.dumps(incident, sort_keys=True, ensure_ascii=False)
        with self._lock:
            self.rotate_incidents_if_needed()
            with self.incident_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        return incident

    def record_from_log(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
        snapshots: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        match = incident_type_from_text(text)
        if not match:
            return {}
        incident_type, severity, subsystem = match
        return self.record_incident(
            incident_type=incident_type,
            severity=severity,
            subsystem=subsystem,
            context={**dict(context or {}), "telegram_error": str(text)[:500]},
            snapshots=snapshots,
            rate_limit_key=f"log:{incident_type}:{str(text)[:160]}",
            raw_text=str(text),
        )

    def read_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        self.ensure()
        lines = self.incident_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        incidents: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                incidents.append(payload)
            if len(incidents) >= max(1, int(limit)):
                break
        return incidents

    def clear_old_incidents(self, keep_latest: int = 100) -> int:
        self.ensure()
        incidents = list(reversed(self.read_incidents(limit=max(1, int(keep_latest)))))
        removed = 0
        with self._lock:
            try:
                current_lines = self.incident_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                removed = max(0, len([line for line in current_lines if line.strip()]) - len(incidents))
                with self.incident_path.open("w", encoding="utf-8", newline="\n") as handle:
                    for incident in incidents:
                        handle.write(json.dumps(incident, sort_keys=True, ensure_ascii=False) + "\n")
            except Exception as exc:
                self.logger(f"Incident manifest cleanup failed: {type(exc).__name__}: {exc}")
                return 0
        return removed

    def latest_incident_report(self) -> dict[str, Any]:
        incidents = self.read_incidents(limit=1)
        return incidents[0] if incidents else {}
