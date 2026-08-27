from __future__ import annotations

from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from codex2lark.core.events import LeasedOutboxMessage


class MessageChannel(Protocol):
    async def send(
        self, to: str, message: dict[str, object], opts: dict[str, object]
    ) -> object: ...


class IMOutboxPublisher:
    _message_kinds = frozenset(
        {"acknowledgement", "progress", "approval", "completed", "blocked", "failed", "cancelled"}
    )

    def __init__(self, channel: MessageChannel) -> None:
        self._channel = channel

    async def publish(self, item: LeasedOutboxMessage) -> str:
        if item.publisher_id != "feishu-im.reply":
            raise ValueError("outbox item is not owned by the Feishu IM publisher")
        if item.message_kind not in self._message_kinds:
            raise ValueError("unsupported Feishu IM result kind")
        chat_id = self._required_text(item.payload, "chat_id")
        message_id = self._required_text(item.payload, "message_id")
        if item.message_kind == "approval":
            card = item.payload.get("card")
            if not isinstance(card, dict):
                raise ValueError("Feishu IM approval outbox payload requires card")
            message: dict[str, object] = {"card": card}
        else:
            message = {"text": self._required_text(item.payload, "text")}
        result = await self._channel.send(
            chat_id,
            message,
            {
                "reply_to": message_id,
                "reply_in_thread": bool(item.payload.get("reply_in_thread", False)),
                "receive_id_type": "chat_id",
                "reply_target_gone": "fail",
                "uuid": self._request_uuid(item.idempotency_key),
            },
        )
        success = bool(getattr(result, "success", False))
        upstream_ref = getattr(result, "message_id", None)
        if not success or not isinstance(upstream_ref, str) or not upstream_ref:
            error: Any = getattr(result, "error", None)
            code = getattr(error, "code", "ambiguous_send")
            raise RuntimeError(f"Feishu IM reply was not confirmed: {code}")
        return upstream_ref

    @staticmethod
    def _required_text(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Feishu IM outbox payload requires {field}")
        return value

    @staticmethod
    def _request_uuid(idempotency_key: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"urn:codex2lark:feishu-im-reply:{idempotency_key}"))
