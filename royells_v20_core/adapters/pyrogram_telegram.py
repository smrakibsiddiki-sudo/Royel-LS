"""Pyrogram-backed TelegramInterface adapter.

Pyrogram is imported by the production bot, but this module uses duck typing
so the Book 17 package remains importable and unit-testable without importing
or starting Telegram clients.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Sequence

from ..errors import TelegramError
from ..interfaces import TelegramInterface


class TelegramPyrogramAdapter(TelegramInterface):
    """Bounded-retry adapter around a supplied Pyrogram Client instance."""

    def __init__(
        self,
        client: Any,
        *,
        request_timeout: float = 120.0,
        retry_base_seconds: float = 1.0,
        retry_cap_seconds: float = 30.0,
    ) -> None:
        self.client = client
        self.request_timeout = max(1.0, float(request_timeout))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.retry_cap_seconds = max(
            self.retry_base_seconds, float(retry_cap_seconds)
        )

    async def _call(self, awaitable: Awaitable[Any]) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout=self.request_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TelegramError(str(exc)) from exc

    async def download_media(self, message: Any, **kwargs: Any) -> Any:
        return await self._call(self.client.download_media(message, **kwargs))

    async def upload_media(self, chat_id: Any, media: Any, **kwargs: Any) -> Any:
        media_kind = str(kwargs.pop("media_kind", "document")).lower()
        method_name = {
            "photo": "send_photo",
            "video": "send_video",
            "audio": "send_audio",
            "voice": "send_voice",
            "animation": "send_animation",
            "document": "send_document",
        }.get(media_kind)
        if method_name is None or not hasattr(self.client, method_name):
            raise TelegramError(f"Unsupported Telegram media kind: {media_kind}")
        return await self._call(
            getattr(self.client, method_name)(chat_id, media, **kwargs)
        )

    async def copy_message(
        self,
        chat_id: Any,
        from_chat_id: Any,
        message_id: int,
        **kwargs: Any,
    ) -> Any:
        return await self._call(
            self.client.copy_message(chat_id, from_chat_id, message_id, **kwargs)
        )

    async def copy_album(
        self,
        chat_id: Any,
        from_chat_id: Any,
        message_ids: Sequence[int],
        **kwargs: Any,
    ) -> Any:
        return await self._call(
            self.client.copy_media_group(
                chat_id,
                from_chat_id,
                list(message_ids),
                **kwargs,
            )
        )

    async def forward(
        self,
        chat_id: Any,
        from_chat_id: Any,
        message_ids: Sequence[int],
        **kwargs: Any,
    ) -> Any:
        return await self._call(
            self.client.forward_messages(
                chat_id,
                from_chat_id,
                list(message_ids),
                **kwargs,
            )
        )

    async def send_album(
        self, chat_id: Any, media: Sequence[Any], **kwargs: Any
    ) -> Any:
        return await self._call(
            self.client.send_media_group(chat_id, list(media), **kwargs)
        )

    async def resolve_peer(self, peer: Any) -> Any:
        return await self._call(self.client.resolve_peer(peer))

    async def get_chat(self, chat_id: Any) -> Any:
        return await self._call(self.client.get_chat(chat_id))

    @staticmethod
    def _flood_wait_seconds(exc: BaseException) -> float | None:
        if exc.__class__.__name__ != "FloodWait":
            return None
        for attribute in ("value", "x", "seconds"):
            value = getattr(exc, attribute, None)
            if value is not None:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    return None
        return None

    async def retry(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        retries: int = 3,
        label: str = "telegram",
    ) -> Any:
        last_error: BaseException | None = None
        for attempt in range(1, max(1, int(retries)) + 1):
            try:
                return await self._call(operation())
            except asyncio.CancelledError:
                raise
            except TelegramError as wrapped:
                last_error = wrapped.__cause__ or wrapped
                if attempt >= max(1, int(retries)):
                    break
                flood_wait = self._flood_wait_seconds(last_error)
                delay = (
                    flood_wait
                    if flood_wait is not None
                    else min(
                        self.retry_cap_seconds,
                        self.retry_base_seconds * (2 ** (attempt - 1)),
                    )
                    + random.uniform(0.0, min(1.0, self.retry_base_seconds))
                )
                await asyncio.sleep(delay)
        raise TelegramError(
            f"{label} failed after {max(1, int(retries))} attempt(s)"
        ) from last_error
