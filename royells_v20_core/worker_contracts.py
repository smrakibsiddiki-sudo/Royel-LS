"""Constructor-injection contracts for production worker refactoring."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .interfaces import (
    AuthorityPolicyInterface,
    CheckpointInterface,
    DeliveryIntentInterface,
    LifecycleInterface,
    LoggerInterface,
    MetricsInterface,
    QueueRegistryInterface,
    RecoveryInterface,
    RuntimeStateInterface,
    StorageInterface,
    TelegramInterface,
)
from .repositories.contracts import (
    ChannelRepositoryInterface,
    DeadMediaRepositoryInterface,
    MetricsRepositoryInterface,
    PostedRepositoryInterface,
    SubscriptionRepositoryInterface,
    TargetMediaRepositoryInterface,
)


@dataclass(frozen=True)
class WorkerDependencies:
    """All infrastructure required by Downloader/Uploader/etc. workers."""

    queues: QueueRegistryInterface
    storage: StorageInterface
    telegram: TelegramInterface | None
    runtime: RuntimeStateInterface
    checkpoint: CheckpointInterface
    recovery: RecoveryInterface
    lifecycle: LifecycleInterface
    authority: AuthorityPolicyInterface
    delivery_intents: DeliveryIntentInterface
    posted: PostedRepositoryInterface
    target_media: TargetMediaRepositoryInterface
    channels: ChannelRepositoryInterface
    subscriptions: SubscriptionRepositoryInterface
    dead_media: DeadMediaRepositoryInterface
    metrics_repository: MetricsRepositoryInterface
    metrics: MetricsInterface
    logger: LoggerInterface


class WorkerInterface(ABC):
    """Common lifecycle contract for injected workers."""

    worker_id: str
    worker_type: str
    restart_on_return: bool = True
    restart_on_error: bool = True
    restart_delay_seconds: float = 5.0

    def __init__(self, dependencies: WorkerDependencies) -> None:
        self.dependencies = dependencies

    @abstractmethod
    async def run(self) -> None:
        """Run until cancelled."""

    async def drain(self) -> None:
        """Flush worker-owned state before cancellation when supported."""

        return None
