from __future__ import annotations

import logging
from typing import Protocol

from .delivery import PartitionedEventDispatcher

logger = logging.getLogger(__name__)


class EventSource(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class EventGateway:
    """Composition boundary for the independently operated realtime service."""

    def __init__(self, source: EventSource, dispatcher: PartitionedEventDispatcher) -> None:
        self.source = source
        self.dispatcher = dispatcher
        self._running = False

    async def start(self) -> None:
        if self._running:
            raise RuntimeError("event gateway is already running")
        await self.dispatcher.start()
        try:
            await self.source.start()
        except Exception:
            await self.dispatcher.stop()
            raise
        self._running = True
        logger.info("event gateway ready")

    async def stop(self) -> None:
        if not self._running:
            return
        try:
            await self.source.stop()
        finally:
            try:
                await self.dispatcher.stop()
            finally:
                self._running = False
                logger.info("event gateway stopped")
