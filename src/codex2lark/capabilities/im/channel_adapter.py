from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, Protocol, cast

from .models import AttachmentReference, IncomingMessage, Mention

logger = logging.getLogger(__name__)


class _DurableDispatcherBridge:
    def __init__(self, converter: Callable[[object], object]) -> None:
        self._converter = converter
        self._message: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._bot_added: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="codex2lark-admission"
        )

    def bind(
        self,
        message: Callable[[dict[str, Any]], Awaitable[None]] | None,
        bot_added: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        self._message = message
        self._bot_added = bot_added

    def dispatch_message(self, data: object) -> None:
        self._dispatch(self._message, data, "message")

    def dispatch_bot_added(self, data: object) -> None:
        self._dispatch(self._bot_added, data, "botAdded")

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _dispatch(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
        data: object,
        event_name: str,
    ) -> None:
        if handler is None:
            raise RuntimeError(f"durable {event_name} admission is not ready")
        raw = self._converter(data)
        if not isinstance(raw, dict):
            raise ValueError(f"durable {event_name} event is not an object")

        async def invoke() -> None:
            await handler(raw)

        future: concurrent.futures.Future[None] = self._executor.submit(
            lambda: asyncio.run(invoke())
        )
        future.result(timeout=25)


class ChannelPort(Protocol):
    bot_identity: object | None

    def on(self, event: str, handler: Callable[[object], Awaitable[None]]) -> object: ...

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None: ...

    async def disconnect(self) -> None: ...

    async def send(self, to: str, message: dict[str, str], opts: dict[str, object]) -> object: ...

    async def download_resource(
        self, resource_key: str, resource_type: str, *, message_id: str
    ) -> bytes | None: ...


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
    from lark_channel.channel import _coerce  # type: ignore[import-untyped]

    class DurableAdmissionChannel(FeishuChannel):  # type: ignore[misc]
        def __init__(self, **parameters: object) -> None:
            super().__init__(**parameters)
            self._durable_bridge = _DurableDispatcherBridge(_coerce.obj_to_dict)

        def bind_durable_handlers(
            self,
            message: Callable[[dict[str, Any]], Awaitable[None]] | None,
            bot_added: Callable[[dict[str, Any]], Awaitable[None]] | None,
        ) -> None:
            self._durable_bridge.bind(message, bot_added)

        def _on_p2_im_message_receive_v1(self, data: object) -> None:
            self._durable_bridge.dispatch_message(data)

        def _on_p2_bot_added(self, data: object) -> None:
            self._durable_bridge.dispatch_bot_added(data)

        async def disconnect(self) -> None:
            try:
                await super().disconnect()
            finally:
                self.close_durable_bridge()

        def close_durable_bridge(self) -> None:
            self._durable_bridge.close()

    return cast(
        ChannelPort,
        DurableAdmissionChannel(
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

    def normalize_raw(self, raw: dict[str, Any], *, received_at_ms: int) -> IncomingMessage:
        header = self._mapping(raw.get("header"))
        event = self._mapping(raw.get("event"))
        message = self._mapping(event.get("message"))
        sender = self._mapping(event.get("sender"))
        sender_id = self._mapping(sender.get("sender_id"))
        content = self._mapping_json(message.get("content"))
        mentions = self._mentions(message.get("mentions"))
        body = self._text(content.get("text"))
        raw_mentions = message.get("mentions")
        for item in raw_mentions if isinstance(raw_mentions, list) else ():
            mention = self._mapping(item)
            identity = self._mapping(mention.get("id"))
            if identity.get("open_id") == self._bot_open_id:
                key = self._text(mention.get("key"))
                if key:
                    body = body.replace(key, "")
        message_type = self._text(message.get("message_type")) or "unknown"
        attachments: list[AttachmentReference] = []
        resource_key = self._text(content.get("file_key") or content.get("image_key"))
        if resource_key:
            attachments.append(
                AttachmentReference(
                    resource_key,
                    "image" if message_type == "image" else "file",
                    self._optional_text(content.get("file_name")),
                )
            )
        return IncomingMessage(
            event_id=self._required_text(header.get("event_id"), "event_id"),
            tenant_key=self._required_text(header.get("tenant_key"), "tenant_key"),
            app_id=self._app_id,
            chat_id=self._required_text(message.get("chat_id"), "chat_id"),
            chat_type=self._text(message.get("chat_type")) or "unknown",
            message_id=self._required_text(message.get("message_id"), "message_id"),
            message_type=message_type,
            sender_id=self._required_text(
                sender_id.get("open_id") or sender_id.get("user_id"), "sender_id"
            ),
            sender_type=self._text(sender.get("sender_type")) or "unknown",
            sender_name=None,
            body_text=" ".join(body.split()),
            mentions=mentions,
            attachments=tuple(attachments),
            occurred_at_ms=self._timestamp_ms(message.get("create_time")),
            received_at_ms=received_at_ms,
            thread_id=self._optional_text(message.get("thread_id")),
            root_id=self._optional_text(message.get("root_id")),
            parent_id=self._optional_text(message.get("parent_id")),
            updated_at_ms=self._optional_timestamp_ms(message.get("update_time")),
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
    def _mapping_json(value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

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
    ) -> None:
        self._channel = channel
        self._admission = admission
        self._app_id = app_id
        self._received_at_ms = received_at_ms
        self._bot_added_handler = bot_added_handler
        self._started = False
        self._ready = asyncio.Event()
        self._normalizer: ChannelMessageNormalizer | None = None

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("official Channel source is already running")
        self._started = True
        self._ready.clear()
        durable_binder = getattr(self._channel, "bind_durable_handlers", None)
        if not callable(durable_binder):
            self._channel.on("message", self._on_message)
            if self._bot_added_handler is not None:
                self._channel.on("botAdded", self._on_bot_added)
        try:
            await self._channel.connect_until_ready(timeout=30.0)
            identity = self._channel.bot_identity
            bot_open_id = getattr(identity, "open_id", None)
            if not isinstance(bot_open_id, str) or not bot_open_id:
                raise RuntimeError("official Channel did not resolve the bot identity")
            self._normalizer = ChannelMessageNormalizer(
                app_id=self._app_id, bot_open_id=bot_open_id
            )
            if callable(durable_binder):
                durable_binder(
                    self._on_raw_message,
                    self._on_raw_bot_added if self._bot_added_handler is not None else None,
                )
            self._ready.set()
        except BaseException:
            self._ready.set()
            self._started = False
            await self._channel.disconnect()
            raise

    async def stop(self) -> None:
        if not self._started:
            return
        durable_binder = getattr(self._channel, "bind_durable_handlers", None)
        if callable(durable_binder):
            durable_binder(None, None)
        await self._channel.disconnect()
        self._started = False
        self._normalizer = None
        self._ready.clear()

    async def _on_message(self, message: object) -> None:
        await self._ready.wait()
        normalizer = self._normalizer
        if normalizer is None:
            raise RuntimeError("official Channel source is not ready for admission")
        normalized = normalizer.normalize(message, received_at_ms=self._received_at_ms())
        await self._admission.admit(normalized)

    async def _on_bot_added(self, event: object) -> None:
        await self._ready.wait()
        if self._normalizer is None or self._bot_added_handler is None:
            raise RuntimeError("official Channel membership source is not ready")
        await self._bot_added_handler.handle_bot_added(event)

    async def _on_raw_message(self, raw: dict[str, Any]) -> None:
        normalizer = self._normalizer
        if normalizer is None:
            raise RuntimeError("official Channel source is not ready for raw admission")
        normalized = normalizer.normalize_raw(raw, received_at_ms=self._received_at_ms())
        await self._admission.admit(normalized)

    async def _on_raw_bot_added(self, raw: dict[str, Any]) -> None:
        if self._bot_added_handler is None:
            raise RuntimeError("official Channel bot-added handler is unavailable")
        event = self._mapping(raw.get("event"))
        await self._bot_added_handler.handle_bot_added(
            SimpleNamespace(raw=raw, chat_id=event.get("chat_id"))
        )

    @staticmethod
    def _mapping(value: object) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}
