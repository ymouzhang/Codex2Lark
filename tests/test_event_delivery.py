from __future__ import annotations

import asyncio

import pytest

from codex2lark.realtime.delivery import (
    EventReference,
    InMemoryTaskQueue,
    PartitionedEventDispatcher,
)


class RecordingHandler:
    def __init__(self, blocked_chat: str) -> None:
        self.blocked_chat = blocked_chat
        self.release = asyncio.Event()
        self.other_handled = asyncio.Event()
        self.calls: list[str] = []

    async def handle(self, reference: EventReference) -> None:
        if reference.chat_id == self.blocked_chat and not self.release.is_set():
            await self.release.wait()
        self.calls.append(reference.event_id)
        if reference.chat_id != self.blocked_chat:
            self.other_handled.set()


def reference(event_id: str, chat_id: str) -> EventReference:
    return EventReference(event_id=event_id, event_type="test", chat_id=chat_id)


@pytest.mark.asyncio
async def test_partitioned_dispatcher_orders_one_chat_and_runs_other_partition() -> None:
    queue = InMemoryTaskQueue(capacity=4)
    handler = RecordingHandler("oc_a")
    dispatcher = PartitionedEventDispatcher(queue, handler, partitions=2)
    other_chat = next(
        f"oc_{index}"
        for index in range(100)
        if dispatcher._partition_index(f"oc_{index}") != dispatcher._partition_index("oc_a")
    )

    await dispatcher.start()
    await queue.publish(reference("a1", "oc_a"))
    await queue.publish(reference("a2", "oc_a"))
    await queue.publish(reference("b1", other_chat))

    await asyncio.wait_for(handler.other_handled.wait(), timeout=1)
    assert handler.calls == ["b1"]

    handler.release.set()
    await dispatcher.stop()
    assert handler.calls == ["b1", "a1", "a2"]


def test_in_memory_queue_and_dispatcher_reject_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="capacity"):
        InMemoryTaskQueue(capacity=0)
    with pytest.raises(ValueError, match="partitions"):
        PartitionedEventDispatcher(InMemoryTaskQueue(), RecordingHandler("oc_a"), partitions=0)
