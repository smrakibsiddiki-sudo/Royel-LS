"""Async redis-py connection and command adapter."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any, Iterable, Mapping, Optional, Sequence

from .configuration import RedisSettings
from .errors import (
    RedisConfigurationError,
    RedisConnectionFailure,
    RedisError,
    RedisTimeout,
)


_COMMAND = re.compile(r"^[A-Z][A-Z0-9_.-]*$")
_DENIED_COMMANDS = frozenset(
    {
        "FLUSHALL",
        "FLUSHDB",
        "CONFIG",
        "SHUTDOWN",
        "DEBUG",
        "MODULE",
        "MIGRATE",
        "SLAVEOF",
        "REPLICAOF",
    }
)


def _classify_error(exc: BaseException) -> RedisError:
    if isinstance(exc, RedisError):
        return exc
    name = exc.__class__.__name__
    message = str(exc)[:1000]
    if "Timeout" in name:
        return RedisTimeout(message)
    if name in {
        "ConnectionError",
        "BusyLoadingError",
        "ClusterDownError",
        "MasterDownError",
    }:
        return RedisConnectionFailure(message)
    return RedisError(message)


class RedisAdapter:
    """Default-disabled async Redis client with owned connection pool."""

    def __init__(
        self,
        settings: RedisSettings,
        *,
        pool_factory: Any = None,
        client_factory: Any = None,
    ) -> None:
        self.settings = settings
        self._pool_factory = pool_factory
        self._client_factory = client_factory
        self._pool: Any = None
        self._client: Any = None
        self._connect_lock = asyncio.Lock()

    def _load_driver(self) -> tuple[Any, Any]:
        if self._pool_factory is not None and self._client_factory is not None:
            return self._pool_factory, self._client_factory
        try:
            import redis.asyncio as redis_async
        except ImportError as exc:
            raise RedisConfigurationError(
                "Book 19 requires the optional redis dependency"
            ) from exc
        return redis_async.BlockingConnectionPool, redis_async.Redis

    async def connect(self) -> None:
        if not self.settings.enabled:
            raise RedisConfigurationError(
                "Redis is disabled; set ENABLE_REDIS_ADAPTER=true only in an approved test environment"
            )
        if not self.settings.redis_url:
            raise RedisConfigurationError("Redis TCP URL is not configured")
        async with self._connect_lock:
            if self._client is not None:
                return
            pool_factory, client_factory = self._load_driver()
            last_error: BaseException | None = None
            for attempt in range(self.settings.retry_attempts):
                pool: Any = None
                client: Any = None
                try:
                    pool = pool_factory.from_url(
                        self.settings.redis_url,
                        max_connections=self.settings.max_connections,
                        timeout=self.settings.pool_wait_timeout_seconds,
                        socket_timeout=self.settings.socket_timeout_seconds,
                        socket_connect_timeout=self.settings.socket_connect_timeout_seconds,
                        health_check_interval=self.settings.health_check_interval_seconds,
                        decode_responses=True,
                    )
                    if hasattr(client_factory, "from_pool"):
                        client = client_factory.from_pool(pool)
                    else:
                        client = client_factory(connection_pool=pool)
                    await client.ping()
                    self._pool = pool
                    self._client = client
                    return
                except Exception as exc:
                    last_error = exc
                    self._pool = None
                    self._client = None
                    if client is not None:
                        with contextlib.suppress(Exception):
                            await client.aclose()
                    if pool is not None:
                        with contextlib.suppress(Exception):
                            await pool.disconnect()
                    if attempt + 1 < self.settings.retry_attempts:
                        delay = min(
                            self.settings.retry_cap_seconds,
                            self.settings.retry_base_seconds * (2**attempt),
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
            if last_error is not None:
                raise _classify_error(last_error) from last_error
            raise RedisConnectionFailure("Redis connection failed")

    async def client(self) -> Any:
        """Return the connected client without exposing pool ownership."""

        if self._client is None:
            await self.connect()
        return self._client

    async def _required_client(self) -> Any:
        return await self.client()

    async def disconnect(self) -> None:
        async with self._connect_lock:
            client, pool = self._client, self._pool
            self._client = None
            self._pool = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.disconnect()

    async def ping(self) -> float:
        client = await self._required_client()
        started = time.perf_counter()
        try:
            if not await client.ping():
                raise RedisConnectionFailure("Redis ping returned false")
            return time.perf_counter() - started
        except RedisError:
            raise
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def health(self) -> Mapping[str, Any]:
        client = await self._required_client()
        ping_latency = await self.ping()
        status: dict[str, Any] = {
            "ok": True,
            "ping_latency_seconds": ping_latency,
        }
        with contextlib.suppress(Exception):
            status["key_count"] = int(await client.dbsize())
        with contextlib.suppress(Exception):
            memory = await client.info("memory")
            status["memory_used_bytes"] = int(memory.get("used_memory", 0))
        with contextlib.suppress(Exception):
            clients = await client.info("clients")
            status["connected_clients"] = int(
                clients.get("connected_clients", 0)
            )
        started = time.perf_counter()
        with contextlib.suppress(Exception):
            pipeline = client.pipeline(transaction=False)
            pipeline.ping()
            pipeline.ping()
            await pipeline.execute()
            status["pipeline_latency_seconds"] = time.perf_counter() - started
        return status

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: Optional[int] = None,
        nx: bool = False,
        xx: bool = False,
    ) -> Any:
        client = await self._required_client()
        try:
            return await client.set(
                key,
                value,
                ex=ttl_seconds,
                nx=nx,
                xx=xx,
            )
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def get(self, key: str) -> Any:
        client = await self._required_client()
        try:
            return await client.get(key)
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        client = await self._required_client()
        try:
            return int(await client.delete(*keys))
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def exists(self, key: str) -> bool:
        client = await self._required_client()
        try:
            return bool(await client.exists(key))
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def expire(self, key: str, ttl_seconds: int) -> bool:
        client = await self._required_client()
        try:
            return bool(await client.expire(key, max(1, int(ttl_seconds))))
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def keys(
        self,
        pattern: str,
        *,
        count: int = 100,
        limit: int = 10000,
    ) -> list[str]:
        """Use non-blocking SCAN iteration; never issue Redis KEYS."""

        client = await self._required_client()
        result: list[str] = []
        try:
            async for key in client.scan_iter(
                match=pattern,
                count=max(1, int(count)),
            ):
                result.append(str(key))
                if len(result) >= max(1, int(limit)):
                    break
            return result
        except Exception as exc:
            raise _classify_error(exc) from exc

    @staticmethod
    def _validate_command(command: Sequence[Any]) -> tuple[Any, ...]:
        if not command:
            raise RedisError("Redis command cannot be empty")
        name = str(command[0]).upper()
        if not _COMMAND.fullmatch(name) or name in _DENIED_COMMANDS:
            raise RedisError(f"Redis command is not allowed: {name}")
        return (name, *command[1:])

    async def pipeline(
        self,
        commands: Iterable[Sequence[Any]],
        *,
        transaction: bool = False,
    ) -> list[Any]:
        client = await self._required_client()
        pipe = client.pipeline(transaction=bool(transaction))
        try:
            for command in commands:
                pipe.execute_command(*self._validate_command(command))
            return list(await pipe.execute())
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def transaction(
        self, commands: Iterable[Sequence[Any]]
    ) -> list[Any]:
        return await self.pipeline(commands, transaction=True)

    async def eval(
        self,
        script: str,
        keys: Sequence[str],
        args: Sequence[Any] = (),
    ) -> Any:
        client = await self._required_client()
        try:
            return await client.eval(script, len(keys), *keys, *args)
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def close(self) -> None:
        await self.disconnect()

    async def publish(self, channel: str, payload: Any) -> int:
        client = await self.client()
        try:
            return int(await client.publish(channel, payload))
        except Exception as exc:
            raise _classify_error(exc) from exc

    async def open_pubsub(self) -> Any:
        client = await self.client()
        try:
            return client.pubsub()
        except Exception as exc:
            raise _classify_error(exc) from exc
