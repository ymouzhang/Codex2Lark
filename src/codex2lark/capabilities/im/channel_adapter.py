from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from .models import AttachmentReference, IncomingMessage, Mention

logger = logging.getLogger(__name__)


class ChannelPort(Protocol):
    bot_identity: object | None

    def on(self, event: str, handler: Callable[[object], Awaitable[None]]) -> object: ...

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None: ...

    async def disconnect(self) -> None: ...

    async def send(self, to: str, message: dict[str, str], opts: dict[str, object]) -> object: ...


class MessageAdmission(Protocol):
    async def admit(self, message: IncomingMessage) -> object: ...


class BotAddedHandler(Protocol):
    async def handle_bot_added(self, event: object) -> None: ...


def create_official_channel(*, app_id: str, app_secret: str) -> ChannelPort:
    from lark_channel import (  # type: ignore[import-untyped]
        ChatQueueConfig,
        DedupConfig,
        FeishuChannel,
        InboundConfig,
        PolicyConfig,
        SafetyConfig,
        SecurityConfig,
        TextBatchConfig,
    )

    return cast(
        ChannelPort,
        FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            transport="ws",
            policy=PolicyConfig(
                dm_policy="disabled",
                group_policy="open",
                require_mention=False,
                respond_to_mention_all=False,
            ),
            safety=SafetyConfig(
                dedup=DedupConfig(enabled=False),
                text_batch=TextBatchConfig(
                    delay_ms=0,
                    long_threshold_chars=10_000,
                    long_delay_ms=0,
                    max_messages=1,
                    max_chars=10_000,
                ),
                chat_queue=ChatQueueConfig(enabled=False),
            ),
            inbound=InboundConfig(
                drop_self_sent=True,
                include_raw=True,
                emit_raw_events=False,
            ),
            security=SecurityConfig(
                mode="strict",
                strict_content_text=True,
                max_ws_fragment_parts=128,
                max_ws_fragment_bytes=8 * 1024 * 1024,
                max_concurrent_ws_handlers=64,
                resource_overflow_policy="drop",
            ),
        ),
    )


class ChannelMessageNormalizer:
    def __init__(self, *, app_id: str, bot_open_id: str) -> None:
        self._app_id = app_id
        self._bot_open_id = bot_open_id

    def normalize(self, value: object, *, received_at_ms: int) -> IncomingMessage:
        raw = self._mapping(getattr(value, "raw", None))
        header = self._mapping(raw.get("header"))
        event = self._mapping(raw.get("event"))
        wire_message = self._mapping(event.get("message"))
        wire_sender = self._mapping(event.get("sender"))
        sender_id = self._text(getattr(value, "sender_id", None))
        sender = getattr(value, "sender", None)
        sender_type = self._text(wire_sender.get("sender_type"))
        if not sender_type:
            sender_type = "bot" if bool(getattr(sender, "is_bot", False)) else "user"

        raw_mentions = wire_message.get("mentions")
        mentions = self._mentions(raw_mentions)
        body = self._body_text(value, wire_message, raw_mentions)
        conversation = getattr(value, "conversation", None)
        chat_type = self._text(getattr(value, "chat_type", None)) or self._text(
            getattr(conversation, "chat_type", None)
        )
        if chat_type == "topic":
            chat_type = "group"
        occurred_at_ms = self._timestamp_ms(getattr(value, "create_time", None))
        updated_at_ms = self._optional_timestamp_ms(wire_message.get("update_time"))
        return IncomingMessage(
            event_id=self._required_text(header.get("event_id"), "event_id"),
            tenant_key=self._required_text(header.get("tenant_key"), "tenant_key"),
            app_id=self._app_id,
            chat_id=self._required_text(getattr(value, "chat_id", None), "chat_id"),
            chat_type=chat_type or "unknown",
            message_id=self._required_text(getattr(value, "message_id", None), "message_id"),
            message_type=self._text(getattr(value, "raw_content_type", None))
            or self._text(wire_message.get("message_type"))
            or "unknown",
            sender_id=self._required_text(sender_id, "sender_id"),
            sender_type=sender_type,
            sender_name=self._optional_text(getattr(value, "sender_name", None)),
            body_text=body,
            mentions=mentions,
            attachments=self._attachments(getattr(value, "resources", None)),
            occurred_at_ms=occurred_at_ms,
            received_at_ms=received_at_ms,
            thread_id=self._optional_text(getattr(conversation, "thread_id", None)),
            root_id=self._optional_text(wire_message.get("root_id")),
            parent_id=self._optional_text(wire_message.get("parent_id")),
            updated_at_ms=updated_at_ms,
        )

    def _body_text(self, value: object, wire_message: dict[str, Any], raw_mentions: object) -> str:
        raw_content = wire_message.get("content")
        content: object = raw_content
        if isinstance(raw_content, str):
            try:
                content = json.loads(raw_content)
            except json.JSONDecodeError:
                content = None
        body = ""
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            body = content["text"]
            for item in raw_mentions if isinstance(raw_mentions, list) else ():
                mention = self._mapping(item)
                identity = self._mapping(mention.get("id"))
                if identity.get("open_id") == self._bot_open_id:
                    key = self._text(mention.get("key"))
                    if key:
                        body = body.replace(key, "")
            return " ".join(body.split())
        safe = self._text(getattr(value, "safe_content_text", None))
        return safe or self._text(getattr(value, "content_text", None))

    @staticmethod
    def _mentions(raw_mentions: object) -> tuple[Mention, ...]:
        if not isinstance(raw_mentions, list):
            return ()
        result: list[Mention] = []
        for item in raw_mentions:
            mention = ChannelMessageNormalizer._mapping(item)
            identity = ChannelMessageNormalizer._mapping(mention.get("id"))
            open_id = ChannelMessageNormalizer._text(identity.get("open_id"))
            if open_id:
                result.append(
                    Mention(open_id, ChannelMessageNormalizer._optional_text(mention.get("name")))
                )
        return tuple(result)

    @staticmethod
    def _attachments(resources: object) -> tuple[AttachmentReference, ...]:
        if not isinstance(resources, list):
            return ()
        result: list[AttachmentReference] = []
        for resource in resources:
            key = ChannelMessageNormalizer._text(getattr(resource, "file_key", None))
            kind = ChannelMessageNormalizer._text(getattr(resource, "type", None))
            if key and kind:
                result.append(
                    AttachmentReference(
                        resource_key=key,
                        resource_type=kind,
                        filename=ChannelMessageNormalizer._optional_text(
                            getattr(resource, "file_name", None)
                        ),
                    )
                )
        return tuple(result)

    @staticmethod
    def _mapping(value: object) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _text(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @classmethod
    def _required_text(cls, value: object, field: str) -> str:
        result = cls._text(value)
        if not result:
            raise ValueError(f"Channel message is missing {field}")
        return result

    @staticmethod
    def _timestamp_ms(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Channel message has an invalid create_time")
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError("Channel message has an invalid create_time") from exc
        return number * 1000 if number < 1_000_000_000_000 else number

    @classmethod
    def _optional_timestamp_ms(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        return cls._timestamp_ms(value)


class OfficialChannelEventSource:
    def __init__(
        self,
        channel: ChannelPort,
        admission: MessageAdmission,
        *,
        app_id: str,
        received_at_ms: Callable[[], int],
        bot_added_handler: BotAddedHandler | None = None,
        capacity: int = 256,
    ) -> None:
        if capacity < 1:
            raise ValueError("Channel event capacity must be positive")
        self._channel = channel
        self._admission = admission
        self._app_id = app_id
        self._received_at_ms = received_at_ms
        self._bot_added_handler = bot_added_handler
        self._queue: asyncio.Queue[object] = asyncio.Queue(capacity)
        self._membership_queue: asyncio.Queue[object] = asyncio.Queue(capacity)
        self._worker: asyncio.Task[None] | None = None
        self._membership_worker: asyncio.Task[None] | None = None
        self._normalizer: ChannelMessageNormalizer | None = None

    async def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("official Channel source is already running")
        self._channel.on("message", self._on_message)
        if self._bot_added_handler is not None:
            self._channel.on("botAdded", self._on_bot_added)
            self._membership_worker = asyncio.create_task(
                self._consume_membership(), name="feishu-im-membership-admission"
            )
        self._worker = asyncio.create_task(self._consume(), name="feishu-im-admission")
        try:
            await self._channel.connect_until_ready(timeout=30.0)
            identity = self._channel.bot_identity
            bot_open_id = getattr(identity, "open_id", None)
            if not isinstance(bot_open_id, str) or not bot_open_id:
                raise RuntimeError("official Channel did not resolve the bot identity")
            self._normalizer = ChannelMessageNormalizer(
                app_id=self._app_id, bot_open_id=bot_open_id
            )
        except BaseException:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
            if self._membership_worker is not None:
                self._membership_worker.cancel()
                await asyncio.gather(self._membership_worker, return_exceptions=True)
                self._membership_worker = None
            await self._channel.disconnect()
            raise

    async def stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        await self._channel.disconnect()
        await self._queue.join()
        await self._membership_queue.join()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if self._membership_worker is not None:
            self._membership_worker.cancel()
            await asyncio.gather(self._membership_worker, return_exceptions=True)
            self._membership_worker = None
        self._worker = None
        self._normalizer = None

    async def _on_message(self, message: object) -> None:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull as exc:
            raise RuntimeError("Feishu IM admission queue is full") from exc

    async def _on_bot_added(self, event: object) -> None:
        try:
            self._membership_queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise RuntimeError("Feishu membership admission queue is full") from exc

    async def _consume(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                while self._normalizer is None:
                    await asyncio.sleep(0)
                normalized = self._normalizer.normalize(
                    message, received_at_ms=self._received_at_ms()
                )
                await self._admission.admit(normalized)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feishu IM message admission failed")
            finally:
                self._queue.task_done()

    async def _consume_membership(self) -> None:
        assert self._bot_added_handler is not None
        while True:
            event = await self._membership_queue.get()
            try:
                await self._bot_added_handler.handle_bot_added(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feishu bot-added admission failed")
            finally:
                self._membership_queue.task_done()
