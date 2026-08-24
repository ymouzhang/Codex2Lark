from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .context_provider import IMContextRequest, MessagePage
from .models import AttachmentReference, IncomingMessage, Mention


@dataclass(frozen=True, slots=True)
class WireMessagePage:
    items: tuple[object, ...]
    has_more: bool
    page_token: str | None = None


class IMMessageAPI(Protocol):
    async def get(self, message_id: str) -> object: ...

    async def list(
        self,
        *,
        container_type: str,
        container_id: str,
        start_time_s: int | None,
        end_time_s: int | None,
        limit: int,
        page_token: str | None = None,
    ) -> WireMessagePage: ...


class OfficialIMMessageAPI:
    def __init__(self, *, app_id: str, app_secret: str) -> None:
        from lark_channel import Client  # type: ignore[import-untyped]

        self._client = Client.builder().app_id(app_id).app_secret(app_secret).build()

    async def get(self, message_id: str) -> object:
        from lark_channel.api.im.v1.model.get_message_request import (  # type: ignore[import-untyped]
            GetMessageRequest,
        )

        response = await self._client.im.v1.message.aget(
            GetMessageRequest.builder().message_id(message_id).build()
        )
        self._require_success(response, "get message")
        items: list[object] = list(getattr(getattr(response, "data", None), "items", None) or ())
        if len(items) != 1:
            raise LookupError(f"Feishu message is unavailable: {message_id}")
        return items[0]

    async def list(
        self,
        *,
        container_type: str,
        container_id: str,
        start_time_s: int | None,
        end_time_s: int | None,
        limit: int,
        page_token: str | None = None,
    ) -> WireMessagePage:
        from lark_channel.api.im.v1.model.list_message_request import (  # type: ignore[import-untyped]
            ListMessageRequest,
        )

        builder = (
            ListMessageRequest.builder()
            .container_id_type(container_type)
            .container_id(container_id)
            .sort_type("ByCreateTimeAsc")
            .page_size(min(50, limit))
        )
        if start_time_s is not None:
            builder = builder.start_time(str(start_time_s))
        if end_time_s is not None:
            builder = builder.end_time(str(end_time_s))
        if page_token:
            builder = builder.page_token(page_token)
        response = await self._client.im.v1.message.alist(builder.build())
        self._require_success(response, "list messages")
        data = getattr(response, "data", None)
        return WireMessagePage(
            tuple(getattr(data, "items", None) or ()),
            bool(getattr(data, "has_more", False)),
            self._optional_text(getattr(data, "page_token", None)),
        )

    @staticmethod
    def _require_success(response: object, operation: str) -> None:
        code = getattr(response, "code", None)
        if code != 0:
            request_id = getattr(response, "request_id", None)
            raise RuntimeError(f"Feishu {operation} failed: code={code}, request_id={request_id}")

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None


class OfficialLiveIMReader:
    def __init__(self, api: IMMessageAPI, *, bot_open_id: str | Callable[[], str | None]) -> None:
        if not bot_open_id:
            raise ValueError("bot_open_id is required")
        self._api = api
        self._bot_open_id = (lambda: bot_open_id) if isinstance(bot_open_id, str) else bot_open_id

    async def get_message(self, request: IMContextRequest) -> IncomingMessage:
        return self._normalize(await self._api.get(request.message_id), request)

    async def related_messages(self, trigger: IncomingMessage, *, limit: int) -> MessagePage:
        conversation_id = trigger.thread_id
        if conversation_id:
            return await self._collect(
                trigger,
                container_type="thread",
                container_id=conversation_id,
                start_time_s=None,
                end_time_s=None,
                limit=limit,
            )
        page = await self._collect(
            trigger,
            container_type="chat",
            container_id=trigger.chat_id,
            start_time_s=None,
            end_time_s=trigger.occurred_at_ms // 1000 + 1,
            limit=limit,
        )
        root = trigger.root_id or trigger.parent_id or trigger.message_id
        return MessagePage(
            tuple(
                item
                for item in page.messages
                if (item.root_id or item.parent_id or item.message_id) == root
            ),
            page.complete,
        )

    async def recent_messages(
        self, trigger: IncomingMessage, *, since_ms: int, limit: int
    ) -> MessagePage:
        return await self._collect(
            trigger,
            container_type="chat",
            container_id=trigger.chat_id,
            start_time_s=since_ms // 1000,
            end_time_s=trigger.occurred_at_ms // 1000 + 1,
            limit=limit,
        )

    async def _collect(
        self,
        trigger: IncomingMessage,
        *,
        container_type: str,
        container_id: str,
        start_time_s: int | None,
        end_time_s: int | None,
        limit: int,
    ) -> MessagePage:
        request = IMContextRequest(
            trigger.tenant_key, trigger.app_id, trigger.chat_id, trigger.message_id
        )
        messages: list[IncomingMessage] = []
        token: str | None = None
        complete = True
        while len(messages) < limit:
            page = await self._api.list(
                container_type=container_type,
                container_id=container_id,
                start_time_s=start_time_s,
                end_time_s=end_time_s,
                limit=limit - len(messages),
                page_token=token,
            )
            messages.extend(self._normalize(item, request) for item in page.items)
            if not page.has_more:
                break
            token = page.page_token
            if not token or len(messages) >= limit:
                complete = False
                break
        return MessagePage(tuple(messages[:limit]), complete)

    def _normalize(self, value: object, request: IMContextRequest) -> IncomingMessage:
        message_id = self._required_text(getattr(value, "message_id", None), "message_id")
        chat_id = self._required_text(getattr(value, "chat_id", None), "chat_id")
        sender = getattr(value, "sender", None)
        sender_id = self._required_text(getattr(sender, "id", None), "sender.id")
        sender_type = self._text(getattr(sender, "sender_type", None)) or "user"
        tenant_key = self._text(getattr(sender, "tenant_key", None)) or request.tenant_key
        message_type = self._text(getattr(value, "msg_type", None)) or "unknown"
        content = self._content(getattr(getattr(value, "body", None), "content", None))
        mentions = tuple(
            Mention(identifier, self._optional_text(getattr(item, "name", None)))
            for item in (getattr(value, "mentions", None) or ())
            if (identifier := self._text(getattr(item, "id", None)))
        )
        body_text, attachments = self._body(message_type, content, mentions, value)
        occurred_at_ms = self._timestamp_ms(getattr(value, "create_time", None))
        updated_at_ms = self._optional_timestamp_ms(getattr(value, "update_time", None))
        return IncomingMessage(
            event_id=f"live:{message_id}:{updated_at_ms or occurred_at_ms}",
            tenant_key=tenant_key,
            app_id=request.app_id,
            chat_id=chat_id,
            chat_type="group",
            message_id=message_id,
            message_type=message_type,
            sender_id=sender_id,
            sender_type=sender_type,
            body_text=body_text,
            mentions=mentions,
            attachments=attachments,
            occurred_at_ms=occurred_at_ms,
            received_at_ms=occurred_at_ms,
            thread_id=self._optional_text(getattr(value, "thread_id", None)),
            root_id=self._optional_text(getattr(value, "root_id", None)),
            parent_id=self._optional_text(getattr(value, "parent_id", None)),
            updated_at_ms=updated_at_ms,
            is_deleted=bool(getattr(value, "deleted", False)),
        )

    def _body(
        self,
        message_type: str,
        content: dict[str, Any],
        mentions: tuple[Mention, ...],
        value: object,
    ) -> tuple[str, tuple[AttachmentReference, ...]]:
        if message_type == "text":
            text = self._text(content.get("text"))
            for mention in mentions:
                if mention.open_id == self._bot_open_id():
                    for item in getattr(value, "mentions", None) or ():
                        if getattr(item, "id", None) == mention.open_id:
                            key = self._text(getattr(item, "key", None))
                            if key:
                                text = text.replace(key, "")
            return " ".join(text.split()), ()
        key_field = "image_key" if message_type == "image" else "file_key"
        key = self._text(content.get(key_field))
        if key:
            filename = self._optional_text(content.get("file_name"))
            return "", (AttachmentReference(key, message_type, filename),)
        return self._text(content.get("text")), ()

    @staticmethod
    def _content(value: object) -> dict[str, Any]:
        if not isinstance(value, str) or not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _text(value: object) -> str:
        return value if isinstance(value, str) else ""

    @classmethod
    def _required_text(cls, value: object, field: str) -> str:
        result = cls._text(value)
        if not result:
            raise ValueError(f"Feishu message is missing {field}")
        return result

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _timestamp_ms(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Feishu message timestamp is invalid")
        result = int(value)
        return result * 1000 if result < 1_000_000_000_000 else result

    @classmethod
    def _optional_timestamp_ms(cls, value: object) -> int | None:
        if value in (None, "", 0):
            return None
        return cls._timestamp_ms(value)
