from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventReference:
    """Minimal routing metadata extracted from an untrusted Feishu event."""

    event_id: str
    event_type: str
    chat_id: str

    @property
    def session_key(self) -> str:
        return self.chat_id


class TaskQueue(Protocol):
    """Queue port; durable deployments may provide another implementation."""

    async def publish(self, reference: EventReference) -> None: ...

    async def receive(self) -> TaskDelivery: ...

    async def join(self) -> None: ...


class EventHandler(Protocol):
    async def handle(self, reference: EventReference) -> None: ...


class TaskDelivery(Protocol):
    """One leased queue item whose completion can map to a durable acknowledgement."""

    reference: EventReference

    async def complete(self) -> None: ...


@dataclass(slots=True)
class _InMemoryDelivery:
    reference: EventReference
    queue: asyncio.Queue[EventReference]
    _completed: bool = False

    async def complete(self) -> None:
        if self._completed:
            raise RuntimeError("task delivery is already complete")
        self.queue.task_done()
        self._completed = True


class InMemoryTaskQueue:
    """Bounded, process-local queue used by the default V2 Lite profile."""

    def __init__(self, *, capacity: int = 256) -> None:
        if capacity < 1:
            raise ValueError("queue capacity must be positive")
        self._queue: asyncio.Queue[EventReference] = asyncio.Queue(maxsize=capacity)

    async def publish(self, reference: EventReference) -> None:
        await self._queue.put(reference)

    async def receive(self) -> TaskDelivery:
        return _InMemoryDelivery(reference=await self._queue.get(), queue=self._queue)

    async def join(self) -> None:
        await self._queue.join()


class PartitionedEventDispatcher:
    """Preserves per-session order while running fixed partitions concurrently."""

    def __init__(
        self,
        queue: TaskQueue,
        handler: EventHandler,
        *,
        partitions: int = 4,
        partition_capacity: int = 64,
    ) -> None:
        if partitions < 1:
            raise ValueError("partitions must be positive")
        if partition_capacity < 1:
            raise ValueError("partition capacity must be positive")
        self.queue = queue
        self.handler = handler
        self._partitions = [
            asyncio.Queue[TaskDelivery](maxsize=partition_capacity) for _ in range(partitions)
        ]
        self._router: asyncio.Task[None] | None = None
        self._workers: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._router is not None:
            raise RuntimeError("event dispatcher is already running")
        self._workers = [
            asyncio.create_task(self._run_partition(partition), name=f"event-partition-{index}")
            for index, partition in enumerate(self._partitions)
        ]
        self._router = asyncio.create_task(self._route(), name="event-router")

    async def stop(self) -> None:
        router = self._router
        if router is None:
            return
        await self.queue.join()
        await asyncio.gather(*(partition.join() for partition in self._partitions))
        router.cancel()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(router, *self._workers, return_exceptions=True)
        self._router = None
        self._workers = []

    async def _route(self) -> None:
        while True:
            delivery = await self.queue.receive()
            await self._partitions[self._partition_index(delivery.reference.session_key)].put(
                delivery
            )

    async def _run_partition(self, partition: asyncio.Queue[TaskDelivery]) -> None:
        while True:
            delivery = await partition.get()
            reference = delivery.reference
            try:
                await self.handler.handle(reference)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "event handler failed; event_id=%s event_type=%s chat_id=%s",
                    reference.event_id,
                    reference.event_type,
                    reference.chat_id,
                )
            finally:
                partition.task_done()
                await delivery.complete()

    def _partition_index(self, session_key: str) -> int:
        digest = hashlib.blake2b(session_key.encode(), digest_size=8).digest()
        return int.from_bytes(digest) % len(self._partitions)
