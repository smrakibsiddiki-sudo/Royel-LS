"""Hybrid Telegram download routing for the Royells v20 compatibility runtime.

The router owns transport selection only. File validation, partial-download
checkpointing, album ordering, cleanup, and queue retry admission remain in
the existing production worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional


class DownloadStatus(str, Enum):
    """Stable result states crossing the hybrid download boundary."""

    SUCCESS = "success"
    NOT_SUPPORTED = "not_supported"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True)
class DownloadRequest:
    """One media download request in source order."""

    message: Any
    destination: str
    timeout_seconds: float = 300.0
    retries: int = 2
    job_id: str = ""
    item_index: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def chat_id(self) -> Any:
        return getattr(getattr(self.message, "chat", None), "id", None)

    @property
    def message_id(self) -> int:
        return int(getattr(self.message, "id", 0) or 0)


@dataclass(frozen=True)
class DownloadResult:
    """Structured adapter result; adapters never leak raw transport errors."""

    status: DownloadStatus
    strategy: str
    path: str = ""
    error: Optional[BaseException] = None
    message: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attempted: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status is DownloadStatus.SUCCESS and bool(self.path)

    @property
    def retryable(self) -> bool:
        return self.status is DownloadStatus.RETRYABLE_FAILURE

    @property
    def permanent(self) -> bool:
        return self.status is DownloadStatus.PERMANENT_FAILURE

    def as_exception(self) -> "HybridDownloadError":
        """Convert the final route result to one worker-compatible exception."""

        if self.error is None:
            error = RuntimeError(
                f"hybrid download {self.status.value} via {self.strategy}"
            )
        else:
            error = self.error
        return HybridDownloadError(
            str(error),
            cause=error,
            retryable=self.retryable,
            permanent=self.permanent,
            not_supported=self.status is DownloadStatus.NOT_SUPPORTED,
            strategy=self.strategy,
            attempted=self.attempted,
        )


class HybridDownloadError(RuntimeError):
    """Final route failure understood by the legacy worker retry logic."""

    def __init__(
        self,
        message: str,
        *,
        cause: Optional[BaseException] = None,
        retryable: bool = True,
        permanent: bool = False,
        not_supported: bool = False,
        strategy: str = "",
        attempted: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.retryable = bool(retryable)
        self.permanent = bool(permanent)
        self.not_supported = bool(not_supported)
        self.strategy = str(strategy)
        self.attempted = tuple(attempted)


DownloadCall = Callable[..., Awaitable[Any]]
ErrorClassifier = Callable[[BaseException], bool]
Logger = Callable[[str], Any]


class DownloadStrategy(ABC):
    """One transport strategy used by :class:`DownloadRouter`."""

    name = "download"

    def __init__(
        self,
        *,
        call: DownloadCall,
        retryable_error: ErrorClassifier,
        permanent_error: ErrorClassifier,
        logger: Optional[Logger] = None,
    ) -> None:
        self.call = call
        self.retryable_error = retryable_error
        self.permanent_error = permanent_error
        self.logger = logger or (lambda _message: None)

    @abstractmethod
    async def download(self, request: DownloadRequest) -> DownloadResult:
        """Attempt one media download."""

    def _failure(
        self,
        request: DownloadRequest,
        error: BaseException,
        *,
        status: Optional[DownloadStatus] = None,
        attempted: tuple[str, ...] = (),
    ) -> DownloadResult:
        if status is None:
            status = (
                DownloadStatus.PERMANENT_FAILURE
                if self.permanent_error(error)
                else DownloadStatus.RETRYABLE_FAILURE
                if self.retryable_error(error)
                else DownloadStatus.NOT_SUPPORTED
            )
        return DownloadResult(
            status=status,
            strategy=self.name,
            error=error,
            message=request.message,
            metadata=dict(request.metadata),
            attempted=attempted or (self.name,),
        )


class BotApiDownloadAdapter(DownloadStrategy):
    """Download through the Bot API, returning NOT_SUPPORTED on access gaps."""

    name = "bot_api"

    _ACCESS_MARKERS = (
        "bot_method_invalid",
        "bot is not a member",
        "channel_private",
        "chat_admin_required",
        "chat not found",
        "chat_id_invalid",
        "channel_invalid",
        "peer_id_invalid",
        "user_not_participant",
        "forbidden",
        "not enough rights",
        "bot cannot",
    )

    def __init__(
        self,
        client: Any,
        *,
        call: DownloadCall,
        retryable_error: ErrorClassifier,
        permanent_error: ErrorClassifier,
        logger: Optional[Logger] = None,
        unsupported_ttl_seconds: float = 900.0,
    ) -> None:
        super().__init__(
            call=call,
            retryable_error=retryable_error,
            permanent_error=permanent_error,
            logger=logger,
        )
        self.client = client
        self.unsupported_ttl_seconds = max(30.0, float(unsupported_ttl_seconds))
        self._unsupported_until: dict[str, float] = {}

    @classmethod
    def _is_access_gap(cls, error: BaseException) -> bool:
        text = f"{type(error).__name__} {error}".lower()
        return any(marker in text for marker in cls._ACCESS_MARKERS)

    def _cache_key(self, request: DownloadRequest) -> str:
        return str(request.chat_id or "")

    def _cached_unsupported(self, request: DownloadRequest) -> bool:
        key = self._cache_key(request)
        until = float(self._unsupported_until.get(key, 0.0))
        if until > time.monotonic():
            return True
        if key:
            self._unsupported_until.pop(key, None)
        return False

    def _remember_unsupported(self, request: DownloadRequest) -> None:
        key = self._cache_key(request)
        if key:
            self._unsupported_until[key] = (
                time.monotonic() + self.unsupported_ttl_seconds
            )

    async def download(self, request: DownloadRequest) -> DownloadResult:
        if self._cached_unsupported(request):
            return DownloadResult(
                status=DownloadStatus.NOT_SUPPORTED,
                strategy=self.name,
                message=request.message,
                metadata={"cached": True, **dict(request.metadata)},
                attempted=(self.name,),
            )
        try:
            bot_message = await self.call(
                "hybrid bot resolve message",
                self.client.get_messages,
                request.chat_id,
                request.message_id,
                retries=1,
                _timeout_seconds=min(30.0, request.timeout_seconds),
                _client_role="bot",
            )
            if not bot_message or getattr(bot_message, "empty", False):
                self._remember_unsupported(request)
                return DownloadResult(
                    status=DownloadStatus.NOT_SUPPORTED,
                    strategy=self.name,
                    message=request.message,
                    metadata={"reason": "bot_message_unavailable", **dict(request.metadata)},
                    attempted=(self.name,),
                )
            path = await self.call(
                "hybrid bot download media",
                self.client.download_media,
                bot_message,
                file_name=request.destination,
                retries=1,
                _timeout_seconds=request.timeout_seconds,
                _client_role="bot",
            )
            if not path:
                raise RuntimeError("Bot API returned no download path")
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                strategy=self.name,
                path=str(path),
                message=request.message,
                metadata={"bot_message": bot_message, **dict(request.metadata)},
                attempted=(self.name,),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._is_access_gap(error):
                self._remember_unsupported(request)
                return DownloadResult(
                    status=DownloadStatus.NOT_SUPPORTED,
                    strategy=self.name,
                    error=error,
                    message=request.message,
                    metadata={"reason": "bot_access_gap", **dict(request.metadata)},
                    attempted=(self.name,),
                )
            return self._failure(
                request,
                error,
                attempted=(self.name,),
            )


class UserbotDownloadAdapter(DownloadStrategy):
    """Reuse the existing Userbot download path without duplicating its logic."""

    name = "userbot"

    def __init__(
        self,
        client: Any,
        *,
        call: DownloadCall,
        retryable_error: ErrorClassifier,
        permanent_error: ErrorClassifier,
        logger: Optional[Logger] = None,
    ) -> None:
        super().__init__(
            call=call,
            retryable_error=retryable_error,
            permanent_error=permanent_error,
            logger=logger,
        )
        self.client = client

    async def download(self, request: DownloadRequest) -> DownloadResult:
        try:
            path = await self.call(
                "hybrid userbot download media",
                self.client.download_media,
                request.message,
                file_name=request.destination,
                retries=max(1, int(request.retries)),
                _timeout_seconds=request.timeout_seconds,
                _client_role="userbot",
            )
            if not path:
                raise RuntimeError("Userbot returned no download path")
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                strategy=self.name,
                path=str(path),
                message=request.message,
                metadata=dict(request.metadata),
                attempted=(self.name,),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._failure(request, error, attempted=(self.name,))


class RetryDownloadAdapter:
    """Classify a dual-transport failure for the existing retry queue."""

    name = "retry_queue"

    def __init__(
        self,
        *,
        retryable_error: ErrorClassifier,
        permanent_error: ErrorClassifier,
    ) -> None:
        self.retryable_error = retryable_error
        self.permanent_error = permanent_error

    def resolve(
        self,
        request: DownloadRequest,
        bot_result: DownloadResult,
        userbot_result: DownloadResult,
    ) -> DownloadResult:
        errors = tuple(
            result.error
            for result in (bot_result, userbot_result)
            if result.error is not None
        )
        final_error = errors[-1] if errors else RuntimeError(
            "Bot API and Userbot could not download media"
        )
        attempted = tuple(
            name
            for name in (
                *(bot_result.attempted or ()),
                *(userbot_result.attempted or ()),
            )
            if name
        )
        if (
            userbot_result.status is DownloadStatus.PERMANENT_FAILURE
            or (
                userbot_result.error is not None
                and self.permanent_error(userbot_result.error)
            )
        ):
            status = DownloadStatus.PERMANENT_FAILURE
        elif (
            userbot_result.status is DownloadStatus.RETRYABLE_FAILURE
            or (
                userbot_result.error is not None
                and self.retryable_error(userbot_result.error)
            )
        ):
            status = DownloadStatus.RETRYABLE_FAILURE
        else:
            status = DownloadStatus.NOT_SUPPORTED
        return DownloadResult(
            status=status,
            strategy=self.name,
            error=final_error,
            message=request.message,
            metadata={
                **dict(request.metadata),
                "bot_status": bot_result.status.value,
                "userbot_status": userbot_result.status.value,
            },
            attempted=attempted or ("bot_api", "userbot"),
        )


class DownloadRouter:
    """Route each media item Bot API -> Userbot -> existing retry queue."""

    def __init__(
        self,
        bot_api: DownloadStrategy,
        userbot: DownloadStrategy,
        retry_queue: RetryDownloadAdapter,
        *,
        logger: Optional[Logger] = None,
    ) -> None:
        self.bot_api = bot_api
        self.userbot = userbot
        self.retry_queue = retry_queue
        self.logger = logger or (lambda _message: None)

    @staticmethod
    def _clear_failed_transport_files(request: DownloadRequest) -> None:
        """Remove partial output before another Telegram client owns the path."""

        for candidate in (
            Path(request.destination),
            Path(f"{request.destination}.temp"),
        ):
            with contextlib.suppress(OSError):
                if candidate.exists() and time.time() - candidate.stat().st_mtime < 30:
                    continue
                candidate.unlink(missing_ok=True)

    async def download(self, request: DownloadRequest) -> DownloadResult:
        bot_result = await self.bot_api.download(request)
        if bot_result.succeeded:
            return bot_result
        self._clear_failed_transport_files(request)
        userbot_result = await self.userbot.download(request)
        if userbot_result.succeeded:
            return DownloadResult(
                status=DownloadStatus.SUCCESS,
                strategy=userbot_result.strategy,
                path=userbot_result.path,
                message=request.message,
                metadata={
                    **dict(bot_result.metadata),
                    **dict(userbot_result.metadata),
                    "bot_status": bot_result.status.value,
                },
                attempted=(
                    *(bot_result.attempted or ("bot_api",)),
                    *(userbot_result.attempted or ("userbot",)),
                ),
            )
        return self.retry_queue.resolve(request, bot_result, userbot_result)

    async def download_many(
        self,
        requests: list[DownloadRequest],
    ) -> list[DownloadResult]:
        """Download in source order so album metadata/order remain unchanged."""

        results: list[DownloadResult] = []
        for request in requests:
            results.append(await self.download(request))
        return results

    async def close(self) -> None:
        """Release optional adapter resources without assuming a client API."""

        for strategy in (self.bot_api, self.userbot):
            closer = getattr(strategy, "close", None)
            if closer is None:
                continue
            with contextlib.suppress(Exception):
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
