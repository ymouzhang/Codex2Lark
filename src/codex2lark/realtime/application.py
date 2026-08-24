from __future__ import annotations

from ..adapters.lark_cli import LarkCli
from ..services.chat_membership import ChatMembershipService
from .delivery import InMemoryTaskQueue, PartitionedEventDispatcher
from .gateway import EventGateway
from .handlers import BotAddedMembershipHandler
from .source import LarkLongConnectionEventSource


def create_gateway(lark: LarkCli | None = None) -> EventGateway:
    """Compose only the services required by the standalone event runtime."""

    client = lark or LarkCli()
    event_queue = InMemoryTaskQueue()
    event_handler = BotAddedMembershipHandler(ChatMembershipService(client))
    event_dispatcher = PartitionedEventDispatcher(event_queue, event_handler)
    event_source = LarkLongConnectionEventSource(client, event_queue.publish)
    return EventGateway(event_source, event_dispatcher)
