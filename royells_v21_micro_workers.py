"""Royells v21 in-process micro-worker engine.

This module keeps the production v20 storage/Telegram recovery contract intact
while splitting ownership boundaries into small event-driven engines.  It is
intentionally Pyrogram-independent so it can be imported during Docker compile
and early startup without touching Telegram sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import os
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence


UNSUPPORTED_TELEGRAM_MEDIA_FIELDS = (
    "animation",
    "document",
    "audio",
    "voice",
    "sticker",
    "contact",
    "poll",
    "dice",
    "game",
    "location",
    "venue",
    "invoice",
    "story",
    "web_page",
)


class JobPriority(str, Enum):
    """Stable priority names requested by the v21 architecture."""

    OWNER = "owner"
    REALTIME = "realtime"
    ALBUM = "album"
    SINGLE = "single"
    RETRY = "retry"
    HISTORICAL = "historical"
    BACKGROUND = "background"


PRIORITY_SCORE = {
    JobPriority.OWNER: 0,
    JobPriority.REALTIME: 10,
    JobPriority.ALBUM: 20,
    JobPriority.SINGLE: 30,
    JobPriority.RETRY: 40,
    JobPriority.HISTORICAL: 50,
    JobPriority.BACKGROUND: 60,
}


class WorkerEventType(str, Enum):
    """Immutable event categories exchanged between micro-worker engines."""

    ADMISSION = "admission"
    SCHEDULE = "schedule"
    DOWNLOAD_ROUTE = "download_route"
    DOWNLOAD_RESULT = "download_result"
    VALIDATION = "validation"
    UPLOAD_ROUTE = "upload_route"
    UPLOAD_RESULT = "upload_result"
    COMMIT = "commit"
    DUPLICATE = "duplicate"
    RUNTIME = "runtime"
    RETRY = "retry"
    METRIC = "metric"
    CLEANUP = "cleanup"
    HEARTBEAT = "heartbeat"
    BACKPRESSURE = "backpressure"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class JobContext:
    """Immutable v21 context attached to every admitted media job."""

    job_id: str
    job_type: str
    source: str
    channel_name: str
    priority: JobPriority
    priority_score: int
    attempt: int
    force_upload: bool
    message_count: int
    media_uids: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["priority"] = self.priority.value
        payload["media_uids"] = list(self.media_uids)
        payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class WorkerEvent:
    """One immutable event published by a worker or coordinator."""

    event_type: WorkerEventType
    source: str
    job_id: str = ""
    worker_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    priority: int = PRIORITY_SCORE[JobPriority.BACKGROUND]
    created_at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_type"] = self.event_type.value
        payload["payload"] = dict(self.payload)
        return payload


@dataclass(frozen=True)
class MediaValidationResult:
    """Media validation output owned by the v21 validation engine."""

    ok: bool
    reason: str
    media_kind: str
    path: str = ""
    size_bytes: int = 0
    sha256: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    code: str = "ok"
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metadata"] = dict(self.metadata)
        return payload


def telegram_media_kind(message: Any) -> str:
    if not message:
        return "none"
    if bool(getattr(message, "photo", None)):
        return "photo"
    if bool(getattr(message, "video", None)):
        return "video"
    return "unsupported"


def is_allowed_telegram_media(message: Any) -> bool:
    """Allow only photo/video; explicitly reject all other Telegram media."""

    if not message:
        return False
    for field_name in UNSUPPORTED_TELEGRAM_MEDIA_FIELDS:
        if bool(getattr(message, field_name, None)):
            return False
    return telegram_media_kind(message) in {"photo", "video"}


def priority_for_job(
    *,
    job_type: str,
    source: str,
    attempt: int = 1,
    force_upload: bool = False,
) -> JobPriority:
    source_text = str(source or "").lower()
    if force_upload or source_text in {"owner", "manual", "telegram_link"}:
        return JobPriority.OWNER
    if int(attempt or 1) > 1:
        return JobPriority.RETRY
    if source_text in {"realtime", "auto_monitor", "source_guard"}:
        return JobPriority.REALTIME
    if str(job_type or "").lower() == "album":
        return JobPriority.ALBUM
    if source_text in {"historical", "historical_backfill", "checkpoint", "recovery"}:
        return JobPriority.HISTORICAL
    if str(job_type or "").lower() == "single":
        return JobPriority.SINGLE
    return JobPriority.BACKGROUND


class EventBus:
    """Small async event bus with per-topic queues and bounded history."""

    def __init__(
        self,
        *,
        queue_size: int = 2000,
        history_size: int = 250,
        logger: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.queue_size = max(10, int(queue_size))
        self.history = deque(maxlen=max(10, int(history_size)))
        self.logger = logger or (lambda _message: None)
        self._queues: dict[WorkerEventType, asyncio.Queue[WorkerEvent]] = {}
        self._counts: Counter[str] = Counter()
        self._dropped: Counter[str] = Counter()
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def queue(self, event_type: WorkerEventType) -> asyncio.Queue[WorkerEvent]:
        event_type = WorkerEventType(event_type)
        queue = self._queues.get(event_type)
        if queue is None:
            queue = asyncio.Queue(maxsize=self.queue_size)
            self._queues[event_type] = queue
        return queue

    def _put_event(self, event: WorkerEvent) -> bool:
        topic = self.queue(event.event_type)
        key = event.event_type.value
        self.history.append(event)
        self._counts[key] += 1
        try:
            topic.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self._dropped[key] += 1
            self.logger(f"v21 event bus topic full; dropped {key} event")
            return False

    def publish_nowait(self, event: WorkerEvent) -> bool:
        event = WorkerEvent(
            event_type=WorkerEventType(event.event_type),
            source=str(event.source),
            job_id=str(event.job_id or ""),
            worker_id=str(event.worker_id or ""),
            payload=dict(event.payload),
            priority=int(event.priority),
            created_at=float(event.created_at),
            event_id=str(event.event_id),
        )
        if self._loop is not None:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    self._loop.call_soon_threadsafe(self._put_event, event)
                    return True
                except RuntimeError:
                    return self._put_event(event)
            if running_loop is not self._loop:
                try:
                    self._loop.call_soon_threadsafe(self._put_event, event)
                    return True
                except RuntimeError:
                    return self._put_event(event)
        return self._put_event(event)

    async def publish(self, event: WorkerEvent) -> bool:
        return self.publish_nowait(event)

    async def get(
        self,
        event_type: WorkerEventType,
        *,
        timeout: Optional[float] = None,
    ) -> Optional[WorkerEvent]:
        topic = self.queue(event_type)
        try:
            if timeout is None:
                return await topic.get()
            return await asyncio.wait_for(topic.get(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            return None

    def task_done(self, event_type: WorkerEventType) -> None:
        with contextlib.suppress(ValueError):
            self.queue(event_type).task_done()

    def drain(self, *, max_per_topic: int = 500) -> int:
        """Drain queued events after they have been counted and snapshotted."""

        drained = 0
        limit = max(1, int(max_per_topic))
        for event_type, topic in list(self._queues.items()):
            for _ in range(limit):
                try:
                    topic.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    drained += 1
                    with contextlib.suppress(ValueError):
                        topic.task_done()
        return drained

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "counts": dict(self._counts),
            "dropped": dict(self._dropped),
            "topics": {
                event_type.value: queue.qsize()
                for event_type, queue in self._queues.items()
            },
            "recent": [event.to_dict() for event in list(self.history)[-25:]],
        }


class AdmissionEngine:
    """Create immutable job contexts and reject non-media before queueing."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    def admit(
        self,
        *,
        job_id: str,
        job_type: str,
        messages: Sequence[Any],
        channel_name: str,
        source: str,
        attempt: int = 1,
        force_upload: bool = False,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> tuple[JobContext, list[Any], list[Any]]:
        allowed: list[Any] = []
        rejected: list[Any] = []
        media_uids: list[str] = []
        for message in messages or []:
            if is_allowed_telegram_media(message):
                allowed.append(message)
                media = getattr(message, "photo", None) or getattr(message, "video", None)
                media_uid = str(getattr(media, "file_unique_id", "") or "")
                if media_uid:
                    media_uids.append(media_uid)
            else:
                rejected.append(message)
        priority = priority_for_job(
            job_type=job_type,
            source=source,
            attempt=attempt,
            force_upload=force_upload,
        )
        context = JobContext(
            job_id=str(job_id),
            job_type=str(job_type),
            source=str(source or "auto"),
            channel_name=str(channel_name or "")[:200],
            priority=priority,
            priority_score=PRIORITY_SCORE[priority],
            attempt=max(1, int(attempt or 1)),
            force_upload=bool(force_upload),
            message_count=len(allowed),
            media_uids=tuple(media_uids),
            metadata=dict(metadata or {}),
        )
        self.bus.publish_nowait(
            WorkerEvent(
                WorkerEventType.ADMISSION,
                source="admission_engine",
                job_id=context.job_id,
                payload={
                    "accepted": len(allowed),
                    "rejected": len(rejected),
                    "priority": priority.value,
                    "trace_id": context.trace_id,
                },
                priority=context.priority_score,
            )
        )
        return context, allowed, rejected


class SchedulerEngine:
    """Own priority metadata, fairness hints, and backpressure decisions."""

    def __init__(
        self,
        bus: EventBus,
        *,
        health_provider: Callable[[], Mapping[str, Any]],
        logger: Callable[[str], Any],
        metric_increment: Callable[..., Any],
    ) -> None:
        self.bus = bus
        self.health_provider = health_provider
        self.logger = logger
        self.metric_increment = metric_increment
        self.last_pause_log: dict[str, float] = {}

    @staticmethod
    def stamp(job: dict[str, Any], context: JobContext, queue_name: str) -> dict[str, Any]:
        job["_v21_context"] = context.to_dict()
        job["_v21_priority"] = context.priority_score
        job["_v21_trace_id"] = context.trace_id
        job["_v21_queue"] = str(queue_name)
        return job

    def pressure_reasons(self) -> list[str]:
        try:
            health = dict(self.health_provider() or {})
        except Exception as exc:
            return [f"health provider failed: {exc}"]
        reasons: list[str] = []
        if bool(health.get("db_busy")):
            reasons.append("database busy")
        if bool(health.get("memory_pressure")):
            reasons.append("memory pressure")
        if bool(health.get("telegram_flood_wait")):
            reasons.append("Telegram FloodWait gate")
        upload_queue = int(health.get("upload_queue") or 0)
        upload_high = int(health.get("upload_queue_high") or 0)
        if upload_high > 0 and upload_queue >= upload_high:
            reasons.append("upload queue high-water")
        if bool(health.get("checkpoint_delayed")):
            reasons.append("checkpoint delayed")
        return reasons

    async def wait_for_download_slot(
        self,
        *,
        job_id: str,
        worker_id: str,
        sleep_seconds: float = 2.0,
    ) -> None:
        while True:
            reasons = self.pressure_reasons()
            if not reasons:
                return
            self.metric_increment("v21_backpressure_pauses")
            key = "|".join(reasons)
            now = time.monotonic()
            if now - float(self.last_pause_log.get(key, 0.0)) >= 30.0:
                self.last_pause_log[key] = now
                self.logger(
                    f"v21 scheduler paused download {job_id}: {', '.join(reasons)}"
                )
            self.bus.publish_nowait(
                WorkerEvent(
                    WorkerEventType.BACKPRESSURE,
                    source="scheduler_engine",
                    job_id=str(job_id),
                    worker_id=str(worker_id),
                    payload={"reasons": reasons},
                )
            )
            await asyncio.sleep(max(0.25, float(sleep_seconds)))

    async def wait_for_source_slot(
        self,
        *,
        source_id: str,
        worker_id: str,
        sleep_seconds: float = 2.0,
    ) -> None:
        while True:
            reasons = self.pressure_reasons()
            if not reasons:
                return
            self.metric_increment("v21_source_backpressure_pauses")
            key = "source|" + "|".join(reasons)
            now = time.monotonic()
            if now - float(self.last_pause_log.get(key, 0.0)) >= 30.0:
                self.last_pause_log[key] = now
                self.logger(
                    f"v21 scheduler paused source intake {source_id}: {', '.join(reasons)}"
                )
            self.bus.publish_nowait(
                WorkerEvent(
                    WorkerEventType.BACKPRESSURE,
                    source="scheduler_engine",
                    job_id=str(source_id),
                    worker_id=str(worker_id),
                    payload={"scope": "source_intake", "reasons": reasons},
                )
            )
            await asyncio.sleep(max(0.25, float(sleep_seconds)))


class DownloadDispatcher:
    """Route downloads only; never mutates DB, retry, or checkpoint state."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    def route(self, *, job_id: str, worker_id: str, strategy: str, metadata: Mapping[str, Any]) -> None:
        self.bus.publish_nowait(
            WorkerEvent(
                WorkerEventType.DOWNLOAD_ROUTE,
                source="download_dispatcher",
                job_id=str(job_id),
                worker_id=str(worker_id),
                payload={"strategy": str(strategy), **dict(metadata or {})},
            )
        )

    def result(
        self,
        *,
        job_id: str,
        worker_id: str,
        strategy: str,
        ok: bool,
        reason: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.bus.publish_nowait(
            WorkerEvent(
                WorkerEventType.DOWNLOAD_RESULT,
                source="download_dispatcher",
                job_id=str(job_id),
                worker_id=str(worker_id),
                payload={
                    "strategy": str(strategy),
                    "ok": bool(ok),
                    "reason": str(reason)[:300],
                    **dict(metadata or {}),
                },
            )
        )


class MediaValidationEngine:
    """Own downloaded-media validation and corruption detection."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    @staticmethod
    def minimum_complete_size(media_kind: str) -> int:
        """Return the first byte length that can pass kind-specific validation.

        The worker pipeline calls this same policy before validation, checkpoint
        recovery, and upload.  Keeping one source of truth prevents a valid small
        photo from passing the validator (513 bytes) but being rejected later by a
        generic 1 KiB gate.
        """

        return 513 if str(media_kind or "").lower() == "photo" else 1025

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _positive_int(value: Any) -> int:
        """Return a positive integer or zero for absent/invalid metadata."""

        try:
            parsed = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return parsed if parsed > 0 else 0

    @classmethod
    def _telegram_media_metadata(cls, message: Any, media_kind: str) -> dict[str, Any]:
        """Extract integrity and display hints from the Telegram source object."""

        media = getattr(message, media_kind, None)
        if media is None:
            return {}
        metadata: dict[str, Any] = {}
        for field_name in ("file_size", "width", "height", "duration"):
            value = cls._positive_int(getattr(media, field_name, 0))
            if value:
                metadata[field_name] = value
        for field_name in ("mime_type", "file_unique_id", "file_name"):
            value = str(getattr(media, field_name, "") or "").strip()
            if value:
                metadata[field_name] = value
        return metadata

    @staticmethod
    def _sample_contains_payload(path: Path, size: int) -> bool:
        """Reject sparse/all-zero artifacts without reading a large media file in full."""

        sample_size = 4096
        offsets = {0, max(0, (size // 2) - (sample_size // 2)), max(0, size - sample_size)}
        with path.open("rb") as handle:
            for offset in sorted(offsets):
                handle.seek(offset)
                if any(handle.read(sample_size)):
                    return True
        return False

    @staticmethod
    def _inspect_iso_bmff(path: Path, size: int) -> tuple[str, dict[str, Any]]:
        """Inspect bounded top-level MP4/MOV boxes without inventing corruption.

        Returns ``valid`` only after the complete top-level box table was parsed,
        ``invalid`` for positively malformed/truncated structures, and
        ``indeterminate`` when the safety bound is reached before EOF.  A bounded
        inspection limit is not evidence that otherwise complete Telegram media is
        corrupt; long fragmented MP4 files can legitimately contain hundreds of
        top-level ``moof``/``mdat`` pairs.
        """

        found: set[str] = set()
        position = 0
        boxes = 0
        error = ""
        max_boxes = 512
        with path.open("rb") as handle:
            while position + 8 <= size and boxes < max_boxes:
                handle.seek(position)
                header = handle.read(16)
                if len(header) < 8:
                    error = "truncated box header"
                    break
                box_size = int.from_bytes(header[0:4], "big", signed=False)
                box_type_bytes = header[4:8]
                try:
                    box_type = box_type_bytes.decode("ascii")
                except UnicodeDecodeError:
                    error = "non-ASCII box type"
                    break
                header_size = 8
                if box_size == 1:
                    if len(header) < 16:
                        error = "truncated extended box header"
                        break
                    box_size = int.from_bytes(header[8:16], "big", signed=False)
                    header_size = 16
                elif box_size == 0:
                    box_size = size - position
                if box_size < header_size:
                    error = f"invalid {box_type!r} box size"
                    break
                next_position = position + box_size
                if next_position > size:
                    error = f"truncated {box_type!r} box"
                    break
                found.add(box_type)
                boxes += 1
                position = next_position
                if position == size:
                    break

        inspection_limited = boxes >= max_boxes and position < size and not error
        if not inspection_limited and not error and position != size:
            error = "truncated trailing box header"

        required = {"ftyp", "mdat"}
        missing = sorted(required.difference(found))
        if not ({"moov", "moof"} & found):
            missing.append("moov/moof")
        if error:
            status = "invalid"
        elif inspection_limited:
            status = "indeterminate"
        elif missing:
            status = "invalid"
        else:
            status = "valid"
        detail = {
            "container": "iso_bmff",
            "container_status": status,
            "container_boxes": sorted(found),
            "container_box_count": boxes,
            "container_box_limit": max_boxes,
            "container_bytes_scanned": position,
            "container_trailing_bytes": max(0, size - position),
            "container_inspection_limited": inspection_limited,
            "container_error": error,
            "container_missing": missing,
        }
        return status, detail

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def validate_file(
        self,
        *,
        message: Any,
        path: str | os.PathLike[str],
        job_id: str,
        worker_id: str,
        photo_validator: Optional[Callable[[str], Any]] = None,
        video_metadata_loader: Optional[Callable[[str], Any]] = None,
    ) -> MediaValidationResult:
        media_kind = telegram_media_kind(message)
        path_text = str(path or "")
        file_path = Path(path_text)
        reason = ""
        metadata: dict[str, Any] = {}
        size = 0
        checksum = ""
        result_code = "validation_exception"
        retryable = False
        validation_ok = False
        try:
            if not is_allowed_telegram_media(message):
                reason = "unsupported Telegram media"
                result_code = "unsupported"
                return MediaValidationResult(
                    False, reason, media_kind, path_text, code=result_code, retryable=False
                )
            if not file_path.exists():
                reason = "downloaded file missing"
                result_code = "missing"
                retryable = True
                return MediaValidationResult(
                    False, reason, media_kind, path_text, code=result_code, retryable=retryable
                )
            size = int(file_path.stat().st_size)
            if media_kind == "photo" and size < self.minimum_complete_size(media_kind):
                reason = "photo file empty or incomplete"
                result_code = "empty" if size <= 0 else "incomplete"
                retryable = True
                return MediaValidationResult(
                    False,
                    reason,
                    media_kind,
                    path_text,
                    size,
                    code=result_code,
                    retryable=retryable,
                )
            if media_kind == "video" and size < self.minimum_complete_size(media_kind):
                reason = "video file empty or incomplete"
                result_code = "empty" if size <= 0 else "incomplete"
                retryable = True
                return MediaValidationResult(
                    False,
                    reason,
                    media_kind,
                    path_text,
                    size,
                    code=result_code,
                    retryable=retryable,
                )

            source_metadata = self._telegram_media_metadata(message, media_kind)
            expected_size = self._positive_int(source_metadata.get("file_size"))
            metadata.update(
                {
                    f"telegram_{key}": value
                    for key, value in source_metadata.items()
                }
            )
            metadata["actual_size_bytes"] = size
            if expected_size:
                metadata["expected_size_bytes"] = expected_size
                if size != expected_size:
                    reason = (
                        f"downloaded file size mismatch: actual={size} expected={expected_size}"
                    )
                    result_code = "incomplete"
                    retryable = True
                    return MediaValidationResult(
                        False,
                        reason,
                        media_kind,
                        path_text,
                        size,
                        metadata=metadata,
                        code=result_code,
                        retryable=retryable,
                    )

            if not self._sample_contains_payload(file_path, size):
                reason = "downloaded file contains no non-zero payload"
                result_code = "empty"
                retryable = True
                return MediaValidationResult(
                    False,
                    reason,
                    media_kind,
                    path_text,
                    size,
                    metadata=metadata,
                    code=result_code,
                    retryable=retryable,
                )

            checksum = await self._maybe_await(asyncio.to_thread(self._hash_file, file_path))
            if media_kind == "photo" and photo_validator is not None:
                ok = bool(await self._maybe_await(photo_validator(path_text)))
                if not ok:
                    reason = "photo decoder rejected file"
                    result_code = "photo_decoder_rejected"
                    retryable = True
                    return MediaValidationResult(
                        False,
                        reason,
                        media_kind,
                        path_text,
                        size,
                        checksum,
                        metadata,
                        code=result_code,
                        retryable=retryable,
                    )

            if media_kind == "video":
                mime_type = str(source_metadata.get("mime_type") or "").lower()
                with file_path.open("rb") as handle:
                    first_header = handle.read(12)
                looks_iso_bmff = len(first_header) >= 8 and first_header[4:8] == b"ftyp"
                expects_iso_bmff = mime_type in {
                    "video/mp4",
                    "video/quicktime",
                    "application/mp4",
                }
                if expects_iso_bmff or looks_iso_bmff:
                    container_status, container_metadata = self._inspect_iso_bmff(file_path, size)
                    metadata.update(container_metadata)
                    if container_status == "invalid":
                        # Telegram's authoritative file_size already matched the completed
                        # local artifact above.  A bounded top-level box walk cannot prove
                        # corruption for every valid MP4/MOV layout (e.g. uncommon atom
                        # ordering, encrypted streams, or vendor-specific metadata).  The
                        # 2026-08-10 incident showed that treating this shallow inspection
                        # as terminal falsely quarantined playable Telegram videos.  Keep
                        # the diagnostic evidence, but never turn it into a delivery veto.
                        metadata["container_advisory"] = (
                            "container structure was not recognized by the bounded inspector; "
                            "Telegram source-size integrity remains authoritative"
                        )
                    if container_status == "indeterminate":
                        metadata["container_advisory"] = (
                            "bounded top-level box inspection limit reached; "
                            "source-size integrity remains authoritative"
                        )

                probe_metadata: dict[str, Any] = {}
                if video_metadata_loader is not None:
                    try:
                        loaded = await self._maybe_await(video_metadata_loader(path_text))
                        probe_metadata.update(dict(loaded or {}))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        metadata["metadata_probe_warning"] = (
                            f"{type(exc).__name__}: {str(exc)[:160]}"
                        )

                probe_width = self._positive_int(probe_metadata.get("width"))
                probe_height = self._positive_int(probe_metadata.get("height"))
                source_width = self._positive_int(source_metadata.get("width"))
                source_height = self._positive_int(source_metadata.get("height"))
                width = probe_width or source_width
                height = probe_height or source_height
                duration = self._positive_int(probe_metadata.get("duration")) or self._positive_int(
                    source_metadata.get("duration")
                )
                metadata.update(probe_metadata)
                metadata.update({"width": width, "height": height, "duration": duration})
                if probe_width and probe_height:
                    metadata["dimension_source"] = "local_probe"
                elif source_width and source_height:
                    metadata["dimension_source"] = "telegram_source"
                else:
                    # Telegram already classified and served this object as a video. Missing
                    # optional display hints are indeterminate, never evidence of corruption.
                    metadata["dimension_source"] = "unavailable"
                    metadata["metadata_advisory"] = "video dimensions unavailable"

            validation_ok = True
            result_code = (
                "ok_metadata_unknown"
                if metadata.get("dimension_source") == "unavailable"
                else "ok"
            )
            result = MediaValidationResult(
                True,
                "ok",
                media_kind,
                path_text,
                size,
                checksum,
                metadata,
                code=result_code,
                retryable=False,
            )
            return result
        except Exception as exc:
            reason = f"validation exception: {type(exc).__name__}: {str(exc)[:160]}"
            result_code = "validation_exception"
            retryable = True
            raise
        finally:
            self.bus.publish_nowait(
                WorkerEvent(
                    WorkerEventType.VALIDATION,
                    source="media_validation_engine",
                    job_id=str(job_id),
                    worker_id=str(worker_id),
                    payload={
                        "path": path_text,
                        "kind": media_kind,
                        "ok": validation_ok,
                        "code": result_code,
                        "retryable": retryable,
                        "reason": reason or "ok",
                        "size_bytes": size,
                        "sha256": checksum,
                        "metadata": dict(metadata),
                    },
                )
            )


class UploadDispatcher:
    """Choose the upload lane only; workers perform the chosen upload action."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    def select_lane(self, job: Mapping[str, Any]) -> str:
        if int(job.get("attempt") or 1) > 1:
            return "retry_upload"
        job_type = str(job.get("type") or "single").lower()
        files = job.get("files") or []
        if not files and job_type == "album":
            return "copy_media_group"
        if not files:
            return "copy_message"
        if job_type == "album":
            return "album_upload"
        return "single_upload"

    def route(self, *, job: Mapping[str, Any], worker_id: str) -> str:
        lane = self.select_lane(job)
        self.bus.publish_nowait(
            WorkerEvent(
                WorkerEventType.UPLOAD_ROUTE,
                source="upload_dispatcher",
                job_id=str(job.get("job_id") or ""),
                worker_id=str(worker_id),
                payload={
                    "lane": lane,
                    "type": str(job.get("type") or ""),
                    "items": len(job.get("messages") or []),
                    "files": len(job.get("files") or []),
                },
            )
        )
        return lane


class DuplicateCoordinator:
    """Single observation point for duplicate decisions made by legacy ledgers."""

    def __init__(self, bus: EventBus, *, metric_increment: Callable[..., Any]) -> None:
        self.bus = bus
        self.metric_increment = metric_increment

    def observe(
        self,
        *,
        uid: str,
        duplicate: bool,
        job_id: str = "",
        source: str = "",
    ) -> None:
        if duplicate:
            self.metric_increment("v21_duplicate_hits")
        self.bus.publish_nowait(
            WorkerEvent(
                WorkerEventType.DUPLICATE,
                source="duplicate_coordinator",
                job_id=str(job_id or ""),
                payload={
                    "uid": str(uid or ""),
                    "duplicate": bool(duplicate),
                    "source": str(source or ""),
                },
            )
        )


class RetryManager:
    """Single owner for retry admission decisions and retry metrics."""

    def __init__(
        self,
        bus: EventBus,
        *,
        metric_increment: Callable[..., Any],
    ) -> None:
        self.bus = bus
        self.metric_increment = metric_increment

    def request(self, *, queue_name: str, job: Mapping[str, Any], delay_seconds: float, reason: Any) -> None:
        self.metric_increment("v21_retry_events")
        self.bus.publish_nowait(
            WorkerEvent(
                WorkerEventType.RETRY,
                source="retry_manager",
                job_id=str((job or {}).get("job_id") or ""),
                payload={
                    "queue": str(queue_name),
                    "delay_seconds": max(0.0, float(delay_seconds or 0.0)),
                    "reason": str(reason)[:300],
                    "attempt": int((job or {}).get("attempt") or (job or {}).get("retries") or 0),
                },
            )
        )


class CleanupEngine:
    """Single owner for temp, cache, and expired runtime file removal."""

    def __init__(
        self,
        bus: EventBus,
        *,
        download_root: str | os.PathLike[str],
        logger: Callable[[str], Any],
        metric_increment: Callable[..., Any],
        grace_seconds: int = 300,
        stale_boot_seconds: int = 120,
    ) -> None:
        self.bus = bus
        self.download_root = Path(download_root)
        self.logger = logger
        self.metric_increment = metric_increment
        self.grace_seconds = max(30, int(grace_seconds))
        self.stale_boot_seconds = max(0, int(stale_boot_seconds))
        self.active_paths: dict[str, Mapping[str, Any]] = {}
        self.deferred: dict[str, float] = {}
        self.last_result: dict[str, Any] = {
            "deleted": 0,
            "freed": 0,
            "skipped_active": 0,
            "errors": 0,
            "updated_at": 0.0,
        }

    @staticmethod
    def _resolve(path: str | os.PathLike[str]) -> str:
        return str(Path(path).expanduser().resolve(strict=False))

    def track(self, path: str | os.PathLike[str], *, owner: str = "", state: str = "active") -> None:
        if not path:
            return
        resolved = self._resolve(path)
        self.active_paths[resolved] = {
            "owner": str(owner or ""),
            "state": str(state or "active"),
            "tracked_at": time.time(),
        }
        self.bus.publish_nowait(
            WorkerEvent(
                WorkerEventType.CLEANUP,
                source="cleanup_engine",
                payload={"action": "track", "path": resolved, "owner": owner, "state": state},
            )
        )

    def untrack(self, paths: Sequence[str | os.PathLike[str]]) -> None:
        changed = 0
        for path in paths or []:
            if not path:
                continue
            resolved = self._resolve(path)
            if self.active_paths.pop(resolved, None) is not None:
                changed += 1
        if changed:
            self.bus.publish_nowait(
                WorkerEvent(
                    WorkerEventType.CLEANUP,
                    source="cleanup_engine",
                    payload={"action": "untrack", "count": changed},
                )
            )

    def defer(self, paths: Sequence[str | os.PathLike[str]], *, reason: str = "") -> None:
        deadline = time.time() + self.grace_seconds
        changed = 0
        for path in paths or []:
            if not path:
                continue
            resolved = self._resolve(path)
            self.active_paths[resolved] = {
                "owner": str(reason or "deferred_cleanup"),
                "state": "deferred",
                "tracked_at": time.time(),
            }
            self.deferred[resolved] = deadline
            changed += 1
        if changed:
            self.bus.publish_nowait(
                WorkerEvent(
                    WorkerEventType.CLEANUP,
                    source="cleanup_engine",
                    payload={"action": "defer", "count": changed, "reason": str(reason)[:200]},
                )
            )

    def cleanup_paths(
        self,
        paths: Sequence[str | os.PathLike[str]],
        *,
        force: bool = False,
        reason: str = "",
    ) -> tuple[int, int, int]:
        deleted = 0
        freed = 0
        skipped_active = 0
        errors = 0
        for path in paths or []:
            if not path:
                continue
            try:
                resolved = self._resolve(path)
                if not force and resolved in self.active_paths:
                    self.deferred[resolved] = time.time() + self.grace_seconds
                    skipped_active += 1
                    continue
                file_path = Path(resolved)
                if not file_path.exists() or not file_path.is_file():
                    self.active_paths.pop(resolved, None)
                    self.deferred.pop(resolved, None)
                    continue
                size = int(file_path.stat().st_size)
                file_path.unlink(missing_ok=True)
                self.active_paths.pop(resolved, None)
                self.deferred.pop(resolved, None)
                deleted += 1
                freed += size
            except Exception:
                errors += 1
        if deleted:
            self.metric_increment("v21_cleanup_deleted", deleted)
        if skipped_active:
            self.metric_increment("v21_cleanup_deferred_active", skipped_active)
        if deleted or skipped_active or errors:
            self.last_result = {
                "deleted": deleted,
                "freed": freed,
                "skipped_active": skipped_active,
                "errors": errors,
                "updated_at": time.time(),
                "reason": str(reason)[:200],
            }
            self.bus.publish_nowait(
                WorkerEvent(
                    WorkerEventType.CLEANUP,
                    source="cleanup_engine",
                    payload={"action": "cleanup", **self.last_result},
                )
            )
        return deleted, freed, skipped_active

    def reap_deferred(self, *, force: bool = False) -> tuple[int, int]:
        now = time.time()
        due = [
            path
            for path, deadline in list(self.deferred.items())
            if force or now >= float(deadline or 0.0)
        ]
        deleted, freed, _skipped = self.cleanup_paths(
            due,
            force=force,
            reason="deferred reap",
        )
        return deleted, freed

    def startup_sweep(
        self,
        *,
        protected_paths: Sequence[str | os.PathLike[str]] = (),
        remove_tiny_files: bool = True,
    ) -> Mapping[str, Any]:
        protected = {
            self._resolve(path)
            for path in protected_paths or []
            if path
        }
        deleted = 0
        freed = 0
        skipped = 0
        candidates: list[Path] = []
        root = self.download_root.resolve(strict=False)
        if not root.exists():
            return {"deleted": 0, "freed": 0, "skipped": 0, "protected": len(protected)}
        now = time.time()
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            resolved = self._resolve(item)
            if resolved in protected:
                skipped += 1
                continue
            name = item.name.lower()
            with contextlib.suppress(OSError):
                age = now - float(item.stat().st_mtime)
                size = int(item.stat().st_size)
                stale = age >= self.stale_boot_seconds
                if self.stale_boot_seconds <= 0:
                    candidates.append(item)
                elif name.endswith(".temp") or name.endswith(".tmp"):
                    candidates.append(item)
                elif remove_tiny_files and stale and size <= 1024:
                    candidates.append(item)
        for item in candidates:
            try:
                resolved = self._resolve(item)
                if resolved in protected:
                    skipped += 1
                    continue
                size = int(item.stat().st_size) if item.exists() else 0
                item.unlink(missing_ok=True)
                deleted += 1
                freed += size
            except Exception:
                skipped += 1
        if deleted:
            self.metric_increment("v21_startup_temp_deleted", deleted)
            self.logger(
                f"v21 cleanup startup sweep removed {deleted} stale temp/tiny file(s), "
                f"freed {freed // (1024 * 1024)} MB."
            )
        self.last_result = {
            "deleted": deleted,
            "freed": freed,
            "skipped_active": skipped,
            "errors": 0,
            "updated_at": time.time(),
            "reason": "startup sweep",
        }
        return {
            "deleted": deleted,
            "freed": freed,
            "skipped": skipped,
            "protected": len(protected),
        }

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "active": len(self.active_paths),
            "deferred": len(self.deferred),
            "download_root": str(self.download_root),
            "last_result": dict(self.last_result),
        }


class UploadRecoveryEngine:
    """Own FILE_PART_X_MISSING recovery orchestration."""

    def __init__(self, bus: EventBus, *, metric_increment: Callable[..., Any]) -> None:
        self.bus = bus
        self.metric_increment = metric_increment

    async def recover(
        self,
        *,
        job: dict[str, Any],
        files: Sequence[str],
        reason: Any,
        reset_callback: Callable[[dict[str, Any], Sequence[str], Any], Awaitable[Any]],
    ) -> Any:
        job_id = str((job or {}).get("job_id") or "")
        self.metric_increment("v21_upload_recovery_events")
        self.bus.publish_nowait(
            WorkerEvent(
                WorkerEventType.RECOVERY,
                source="upload_recovery_engine",
                job_id=job_id,
                payload={
                    "action": "recreate_upload_session",
                    "files": [str(path) for path in files or []],
                    "reason": str(reason)[:300],
                },
            )
        )
        return await reset_callback(job, files, reason)


class MicroWorkerEngine:
    """Facade combining all v21 single-responsibility engines."""

    def __init__(
        self,
        *,
        download_root: str | os.PathLike[str],
        health_provider: Callable[[], Mapping[str, Any]],
        logger: Callable[[str], Any],
        metric_increment: Callable[..., Any],
        cleanup_grace_seconds: int = 300,
        stale_boot_seconds: int = 120,
        event_queue_size: int = 2000,
    ) -> None:
        self.started_at = time.time()
        self.logger = logger
        self.metric_increment = metric_increment
        self.bus = EventBus(queue_size=event_queue_size, logger=logger)
        self.admission = AdmissionEngine(self.bus)
        self.scheduler = SchedulerEngine(
            self.bus,
            health_provider=health_provider,
            logger=logger,
            metric_increment=metric_increment,
        )
        self.download_dispatcher = DownloadDispatcher(self.bus)
        self.media_validation = MediaValidationEngine(self.bus)
        self.upload_dispatcher = UploadDispatcher(self.bus)
        self.duplicate_coordinator = DuplicateCoordinator(
            self.bus,
            metric_increment=metric_increment,
        )
        self.retry_manager = RetryManager(self.bus, metric_increment=metric_increment)
        self.cleanup_engine = CleanupEngine(
            self.bus,
            download_root=download_root,
            logger=logger,
            metric_increment=metric_increment,
            grace_seconds=cleanup_grace_seconds,
            stale_boot_seconds=stale_boot_seconds,
        )
        self.upload_recovery = UploadRecoveryEngine(
            self.bus,
            metric_increment=metric_increment,
        )

    def admit_legacy_job(
        self,
        *,
        job_id: str,
        job_type: str,
        messages: Sequence[Any],
        channel_name: str,
        source: str,
        attempt: int = 1,
        force_upload: bool = False,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> tuple[JobContext, list[Any], list[Any]]:
        return self.admission.admit(
            job_id=job_id,
            job_type=job_type,
            messages=messages,
            channel_name=channel_name,
            source=source,
            attempt=attempt,
            force_upload=force_upload,
            metadata=metadata,
        )

    def annotate_job(self, job: dict[str, Any], context: JobContext, queue_name: str) -> dict[str, Any]:
        return self.scheduler.stamp(job, context, queue_name)

    async def wait_for_download_capacity(self, *, job: Mapping[str, Any], worker_id: str) -> None:
        await self.scheduler.wait_for_download_slot(
            job_id=str((job or {}).get("job_id") or ""),
            worker_id=worker_id,
        )

    async def wait_for_source_capacity(self, *, source_id: str, worker_id: str) -> None:
        await self.scheduler.wait_for_source_slot(
            source_id=str(source_id or ""),
            worker_id=str(worker_id or ""),
        )

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "architecture": "v21-micro-worker-engine",
            "uptime_seconds": max(0.0, time.time() - self.started_at),
            "events": self.bus.snapshot(),
            "cleanup": self.cleanup_engine.snapshot(),
        }
