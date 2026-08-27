from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol, cast

from .models import AttachmentReference, IncomingMessage, Mention

logger = logging.getLogger(__name__)

_WEBSOCKET_URL = re.compile(r"\bwss?://[^\s]+", re.IGNORECASE)


class _ChannelWebSocketURLFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _WEBSOCKET_URL.sub("<redacted websocket endpoint>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _install_channel_log_redaction(channel_logger: logging.Logger) -> None:
    if any(isinstance(item, _ChannelWebSocketURLFilter) for item in channel_logger.filters):
        return
    channel_logger.addFilter(_ChannelWebSocketURLFilter())


class _DurableDispatcherBridge:
    def __init__(self, converter: Callable[[object], object]) -> None:
        self._converter = converter
        self._message: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._bot_added: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._message_recalled: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._bot_removed: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._card_action: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._futures: set[concurrent.futures.Future[None]] = set()
        self._lock = threading.Lock()

    def bind_runtime_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        if loop is not None and (loop.is_closed() or not loop.is_running()):
            raise RuntimeError("durable admission requires a running Runtime loop")
        with self._lock:
            self._runtime_loop = loop

    def bind(
        self,
        message: Callable[[dict[str, Any]], Awaitable[None]] | None,
        bot_added: Callable[[dict[str, Any]], Awaitable[None]] | None,
        message_recalled: Callable[[dict[str, Any]], Awaitable[None]] | None,
        bot_removed: Callable[[dict[str, Any]], Awaitable[None]] | None,
        card_action: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        self._message = message
        self._bot_added = bot_added
        self._message_recalled = message_recalled
        self._bot_removed = bot_removed
        self._card_action = card_action

    def dispatch_message(self, data: object) -> None:
        self._dispatch(self._message, data, "message")

    def dispatch_bot_added(self, data: object) -> None:
        self._dispatch(self._bot_added, data, "botAdded")

    def dispatch_message_recalled(self, data: object) -> None:
        self._dispatch(self._message_recalled, data, "messageRecalled")

    def dispatch_bot_removed(self, data: object) -> None:
        self._dispatch(self._bot_removed, data, "botLeave")

    def dispatch_card_action(self, data: object) -> None:
        self._dispatch(self._card_action, data, "cardAction")

    def close(self) -> None:
        with self._lock:
            self._runtime_loop = None
            futures = tuple(self._futures)
        for future in futures:
            future.cancel()

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

        with self._lock:
            runtime_loop = self._runtime_loop
        if runtime_loop is None or runtime_loop.is_closed() or not runtime_loop.is_running():
            raise RuntimeError("durable Runtime event loop is not ready")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is runtime_loop:
            raise RuntimeError("durable SDK callback cannot block the Runtime event loop")

        async def invoke() -> None:
            await handler(raw)

        future = asyncio.run_coroutine_threadsafe(invoke(), runtime_loop)
        with self._lock:
            self._futures.add(future)
        try:
            future.result(timeout=25)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"durable {event_name} admission timed out") from None
        finally:
            with self._lock:
                self._futures.discard(future)


class ChannelPort(Protocol):
    bot_identity: object | None

    def on(self, event: str, handler: Callable[..., object]) -> object: ...

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None: ...

    async def disconnect(self) -> None: ...

    async def send(
        self, to: str, message: dict[str, object], opts: dict[str, object]
    ) -> object: ...

    async def download_resource(
        self, resource_key: str, resource_type: str, *, message_id: str
    ) -> bytes | None: ...


class MessageAdmission(Protocol):
    async def admit(self, message: IncomingMessage) -> object: ...


class BotAddedHandler(Protocol):
    async def handle_bot_added(self, event: object) -> None: ...


class LifecycleHandler(Protocol):
    async def handle_message_recalled(self, raw: dict[str, Any]) -> None: ...

    async def handle_bot_removed(self, raw: dict[str, Any]) -> None: ...


class CardActionHandler(Protocol):
    async def handle(self, raw: dict[str, Any]) -> str: ...


@dataclass(frozen=True, slots=True)
class EventSourceHealth:
    state: str
    ready: bool
    reconnect_attempts: int
    version: int


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
    from lark_channel.core.log import logger as channel_logger  # type: ignore[import-untyped]

    _install_channel_log_redaction(channel_logger)

    class DurableAdmissionChannel(FeishuChannel):  # type: ignore[misc]
        def __init__(self, **parameters: object) -> None:
            super().__init__(**parameters)
            self._durable_bridge = _DurableDispatcherBridge(_coerce.obj_to_dict)

        def bind_durable_handlers(
            self,
            message: Callable[[dict[str, Any]], Awaitable[None]] | None,
            bot_added: Callable[[dict[str, Any]], Awaitable[None]] | None,
            message_recalled: Callable[[dict[str, Any]], Awaitable[None]] | None,
            bot_removed: Callable[[dict[str, Any]], Awaitable[None]] | None,
            card_action: Callable[[dict[str, Any]], Awaitable[None]] | None,
        ) -> None:
            self._durable_bridge.bind(
                message, bot_added, message_recalled, bot_removed, card_action
            )

        def bind_runtime_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
            self._durable_bridge.bind_runtime_loop(loop)

        def _build_dispatcher(self) -> object:
            dispatcher = super()._build_dispatcher()
            from lark_channel.event.custom import (  # type: ignore[import-untyped]
                CustomizedEventProcessor,
            )

            processors = getattr(dispatcher, "_processorMap", None)
            if not isinstance(processors, dict):
                raise RuntimeError("official Channel dispatcher does not expose event processors")
            processors["p2.im.message.recalled_v1"] = CustomizedEventProcessor(
                self._on_p2_message_recalled
            )
            return dispatcher

        def _on_p2_im_message_receive_v1(self, data: object) -> None:
            self._durable_bridge.dispatch_message(data)

        def _on_p2_bot_added(self, data: object) -> None:
            self._durable_bridge.dispatch_bot_added(data)

        def _on_p2_message_recalled(self, data: object) -> None:
            self._durable_bridge.dispatch_message_recalled(data)

        def _on_p2_bot_deleted(self, data: object) -> None:
            self._durable_bridge.dispatch_bot_removed(data)

        def _on_p2_card_action_trigger(self, data: object) -> object:
            from lark_channel.event.callback.model.p2_card_action_trigger import (  # type: ignore[import-untyped]
                P2CardActionTriggerResponse,
            )

            self._durable_bridge.dispatch_card_action(data)
            return P2CardActionTriggerResponse({})

        def start(self) -> None:
            from lark_channel.ws.client import (  # type: ignore[import-untyped]
                loop as transport_loop,
            )

            asyncio.set_event_loop(transport_loop)
            try:
                super().start()
            finally:
                asyncio.set_event_loop(None)

        def stop(self, *, join_timeout: float = 5.0) -> None:
            self._cancel_transport_periodic_tasks()
            super().stop(join_timeout=join_timeout)

        def _cancel_transport_periodic_tasks(self) -> None:
            ws_client = getattr(self, "_ws_client", None)
            cache = getattr(ws_client, "_cache", None)
            cron = getattr(cache, "_cron", None)
            if not isinstance(cron, asyncio.Task):
                return
            transport_loop = cron.get_loop()
            if not transport_loop.is_running():
                cron.cancel()
                return

            async def cancel_periodic_tasks() -> None:
                tasks = [
                    task
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                    and getattr(task.get_coro(), "__qualname__", "")
                    in {"Client._ping_loop", "ExpiringCache._start_clear_cron"}
                ]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            future = asyncio.run_coroutine_threadsafe(cancel_periodic_tasks(), transport_loop)
            with suppress(concurrent.futures.CancelledError, TimeoutError):
                future.result(timeout=1.0)

        def _drain_cancelled_bg_tasks(self) -> None:
            background_loop = getattr(self, "_bg_loop", None)
            if background_loop is None or not background_loop.is_running():
                return
            drained = threading.Event()
            try:
                background_loop.call_soon_threadsafe(drained.set)
            except RuntimeError:
                return
            drained.wait(timeout=0.5)

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
        lifecycle_handler: LifecycleHandler | None = None,
        card_action_handler: CardActionHandler | None = None,
    ) -> None:
        self._channel = channel
        self._admission = admission
        self._app_id = app_id
        self._received_at_ms = received_at_ms
        self._bot_added_handler = bot_added_handler
        self._lifecycle_handler = lifecycle_handler
        self._card_action_handler = card_action_handler
        self._started = False
        self._ready = asyncio.Event()
        self._normalizer: ChannelMessageNormalizer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._health = EventSourceHealth("stopped", False, 0, 0)
        self._health_changed = asyncio.Event()
        self._transport_subscriptions: list[Callable[[], object]] = []

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("official Channel source is already running")
        self._started = True
        self._ready.clear()
        self._loop = asyncio.get_running_loop()
        runtime_loop_binder = getattr(self._channel, "bind_runtime_loop", None)
        self._set_health("starting", ready=False)
        durable_binder = getattr(self._channel, "bind_durable_handlers", None)
        try:
            if callable(runtime_loop_binder):
                runtime_loop_binder(self._loop)
            for event, handler in (
                ("reconnecting", self._on_transport_reconnecting),
                ("reconnected", self._on_transport_reconnected),
            ):
                unsubscribe = self._channel.on(event, handler)
                if callable(unsubscribe):
                    self._transport_subscriptions.append(unsubscribe)
            if not callable(durable_binder):
                self._channel.on("message", self._on_message)
                if self._bot_added_handler is not None:
                    self._channel.on("botAdded", self._on_bot_added)
                if self._lifecycle_handler is not None:
                    self._channel.on("botLeave", self._on_bot_removed)
                if self._card_action_handler is not None:
                    self._channel.on("cardAction", self._on_card_action)
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
                    (
                        self._on_raw_message_recalled
                        if self._lifecycle_handler is not None
                        else None
                    ),
                    (self._on_raw_bot_removed if self._lifecycle_handler is not None else None),
                    self._on_raw_card_action if self._card_action_handler is not None else None,
                )
            self._ready.set()
            self._set_health("connected", ready=True)
        except BaseException:
            self._set_health("error", ready=False)
            self._started = False
            try:
                await self._channel.disconnect()
            finally:
                if callable(runtime_loop_binder):
                    runtime_loop_binder(None)
                self._unsubscribe_transport()
            raise

    async def stop(self) -> None:
        if not self._started:
            return
        durable_binder = getattr(self._channel, "bind_durable_handlers", None)
        if callable(durable_binder):
            durable_binder(None, None, None, None, None)
        runtime_loop_binder = getattr(self._channel, "bind_runtime_loop", None)
        try:
            await self._channel.disconnect()
        finally:
            if callable(runtime_loop_binder):
                runtime_loop_binder(None)
            self._started = False
            self._normalizer = None
            self._ready.clear()
            self._set_health("stopped", ready=False)
            self._unsubscribe_transport()
            self._loop = None

    def health(self) -> EventSourceHealth:
        return self._health

    async def wait_health_change(self, after_version: int) -> EventSourceHealth:
        while self._health.version == after_version:
            self._health_changed.clear()
            if self._health.version != after_version:
                break
            await self._health_changed.wait()
        return self._health

    def _on_transport_reconnecting(self, *_args: object) -> None:
        self._schedule_health("reconnecting", ready=False, reconnect=True)

    def _on_transport_reconnected(self, *_args: object) -> None:
        self._schedule_health("connected", ready=True)

    def _schedule_health(self, state: str, *, ready: bool, reconnect: bool = False) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(
            self._set_health,
            state,
            ready,
            reconnect,
        )

    def _set_health(self, state: str, ready: bool, reconnect: bool = False) -> None:
        attempts = self._health.reconnect_attempts + (1 if reconnect else 0)
        if ready:
            self._ready.set()
        else:
            self._ready.clear()
        self._health = EventSourceHealth(
            state,
            ready,
            attempts,
            self._health.version + 1,
        )
        self._health_changed.set()

    def _unsubscribe_transport(self) -> None:
        for unsubscribe in self._transport_subscriptions:
            try:
                unsubscribe()
            except Exception:
                logger.warning("Channel transport-event unsubscribe failed")
        self._transport_subscriptions.clear()

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
        await self._ready.wait()
        normalizer = self._normalizer
        if normalizer is None:
            raise RuntimeError("official Channel source is not ready for raw admission")
        normalized = normalizer.normalize_raw(raw, received_at_ms=self._received_at_ms())
        await self._admission.admit(normalized)

    async def _on_raw_bot_added(self, raw: dict[str, Any]) -> None:
        await self._ready.wait()
        if self._bot_added_handler is None:
            raise RuntimeError("official Channel bot-added handler is unavailable")
        event = self._mapping(raw.get("event"))
        await self._bot_added_handler.handle_bot_added(
            SimpleNamespace(raw=raw, chat_id=event.get("chat_id"))
        )

    async def _on_raw_message_recalled(self, raw: dict[str, Any]) -> None:
        await self._ready.wait()
        if self._lifecycle_handler is None:
            raise RuntimeError("official Channel lifecycle handler is unavailable")
        await self._lifecycle_handler.handle_message_recalled(raw)

    async def _on_raw_bot_removed(self, raw: dict[str, Any]) -> None:
        await self._ready.wait()
        if self._lifecycle_handler is None:
            raise RuntimeError("official Channel lifecycle handler is unavailable")
        await self._lifecycle_handler.handle_bot_removed(raw)

    async def _on_raw_card_action(self, raw: dict[str, Any]) -> str:
        await self._ready.wait()
        if self._card_action_handler is None:
            raise RuntimeError("official Channel approval handler is unavailable")
        return await self._card_action_handler.handle(raw)

    async def _on_bot_removed(self, event: object) -> None:
        await self._ready.wait()
        if self._lifecycle_handler is None:
            raise RuntimeError("official Channel lifecycle handler is unavailable")
        raw = getattr(event, "raw", None)
        if not isinstance(raw, dict):
            raise ValueError("bot-removed event does not expose its raw envelope")
        await self._lifecycle_handler.handle_bot_removed(raw)

    async def _on_card_action(self, event: object) -> None:
        await self._ready.wait()
        if self._card_action_handler is None:
            raise RuntimeError("official Channel approval handler is unavailable")
        raw = getattr(event, "raw", None)
        if not isinstance(raw, dict):
            raise ValueError("card action event does not expose its raw envelope")
        await self._card_action_handler.handle(raw)

    @staticmethod
    def _mapping(value: object) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}
