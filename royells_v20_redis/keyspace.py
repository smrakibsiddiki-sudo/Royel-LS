"""Versioned and namespace-safe Redis key construction."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import RedisConfigurationError


_KEY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class QueueKeys:
    ready: str
    scheduled: str
    processing: str
    dead: str
    completed: str
    payloads: str
    checksums: str
    priorities: str
    members: str
    attempts: str
    created: str
    available: str
    errors: str
    owners: str
    sequence: str
    metrics: str

    def all(self) -> tuple[str, ...]:
        return tuple(self.__dict__.values())


class RedisKeyspace:
    """Canonical `prefix:version:domain:name` key builder."""

    def __init__(
        self,
        prefix: str = "royells",
        version: str = "v1",
        queue_prefix: str = "queue",
    ) -> None:
        self.prefix = self._part(prefix)
        self.version = self._part(version)
        self.queue_prefix = self._part(queue_prefix)

    @staticmethod
    def _part(value: str) -> str:
        normalized = str(value or "").strip()
        if not _KEY_PART.fullmatch(normalized):
            raise RedisConfigurationError(f"Unsafe Redis key part: {value!r}")
        return normalized

    def key(self, *parts: str) -> str:
        return ":".join(
            (self.prefix, self.version, *(self._part(part) for part in parts))
        )

    def tagged_key(
        self,
        domain: str,
        name: str,
        *parts: str,
    ) -> str:
        """Build a Redis Cluster-compatible key for one atomic scope."""

        safe_domain = self._part(domain)
        safe_name = self._part(name)
        suffix = tuple(self._part(part) for part in parts)
        return ":".join(
            (
                self.prefix,
                self.version,
                safe_domain,
                f"{{{safe_name}}}",
                *suffix,
            )
        )

    def queue(self, name: str) -> QueueKeys:
        queue_name = self._part(name)
        base = self.tagged_key(self.queue_prefix, queue_name)
        return QueueKeys(
            ready=f"{base}:ready",
            scheduled=f"{base}:scheduled",
            processing=f"{base}:processing",
            dead=f"{base}:dead",
            completed=f"{base}:completed",
            payloads=f"{base}:payloads",
            checksums=f"{base}:checksums",
            priorities=f"{base}:priorities",
            members=f"{base}:members",
            attempts=f"{base}:attempts",
            created=f"{base}:created",
            available=f"{base}:available",
            errors=f"{base}:errors",
            owners=f"{base}:owners",
            sequence=f"{base}:sequence",
            metrics=f"{base}:metrics",
        )

    def runtime(self, name: str) -> str:
        return self.tagged_key("runtime", name, "state")

    def heartbeat(self, component: str) -> str:
        return self.tagged_key("heartbeat", component, "state")

    def worker(self, worker_id: str) -> str:
        return self.tagged_key("workers", worker_id, "state")

    def processing(self, job_id: str) -> str:
        return self.tagged_key("processing", job_id, "state")

    def checkpoint(self, name: str = "runtime_checkpoint") -> str:
        return self.tagged_key("checkpoint", name, "state")

    def lock(self, name: str) -> str:
        return self.tagged_key("lock", name, "lease")

    def fence(self, name: str) -> str:
        return self.tagged_key("lock", name, "fence")

    def metrics(self, name: str = "global") -> str:
        return self.key("metrics", name)

    def event_channel(self, name: str) -> str:
        return self.key("events", name)
