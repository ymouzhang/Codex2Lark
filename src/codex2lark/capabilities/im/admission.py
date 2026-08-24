from __future__ import annotations

import json
from typing import Protocol

from codex2lark.core.events import NormalizedEvent, OutboxDraft, TaskCommand
from codex2lark.core.ids import new_trace_id
from codex2lark.storage.runtime_store import RuntimeStore

from .models import IMAdmissionDecision, IMAdmissionReason, IncomingMessage


class MessageMirror(Protocol):
    async def upsert_message(self, message: IncomingMessage) -> None: ...


class IMAdmissionService:
    def __init__(
        self,
        runtime_store: RuntimeStore,
        message_mirror: MessageMirror,
        *,
        bot_open_id: str,
        acknowledgement_text: str,
    ) -> None:
        if not bot_open_id:
            raise ValueError("bot_open_id is required")
        if not acknowledgement_text.strip():
            raise ValueError("acknowledgement_text is required")
        self._runtime_store = runtime_store
        self._message_mirror = message_mirror
        self._bot_open_id = bot_open_id
        self._acknowledgement_text = acknowledgement_text

    async def admit(self, message: IncomingMessage) -> IMAdmissionDecision:
        reason = self._evaluate(message)
        if reason is not IMAdmissionReason.ADMITTED:
            return IMAdmissionDecision(reason)

        await self._message_mirror.upsert_message(message)
        source_payload = json.dumps(
            {
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "sender_id": message.sender_id,
                "thread_id": message.thread_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        event = NormalizedEvent(
            event_id=message.event_id,
            plugin_id="feishu-im",
            event_type="im.message.receive_v1",
            tenant_key=message.tenant_key,
            app_id=message.app_id,
            occurred_at_ms=message.occurred_at_ms,
            received_at_ms=message.received_at_ms,
            resource_kind="im.message",
            resource_id=message.message_id,
            correlation_id=message.thread_id or message.root_id,
            trace_id=str(new_trace_id()),
            source_payload=source_payload,
            payload_expires_at_ms=message.received_at_ms + 7 * 24 * 60 * 60 * 1000,
        )
        command = TaskCommand(
            plugin_id="feishu-im",
            command_type="im.handle_mention",
            session_key=message.session_key,
            payload={
                "tenant_key": message.tenant_key,
                "app_id": message.app_id,
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "thread_id": message.thread_id,
                "request": message.body_text.strip(),
            },
            available_at_ms=message.received_at_ms,
        )
        acknowledgement = OutboxDraft(
            publisher_id="feishu-im.reply",
            destination_ref=message.message_id,
            message_kind="acknowledgement",
            idempotency_key=self._reply_key(message, "acknowledgement"),
            payload={
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "reply_in_thread": message.thread_id is not None,
                "text": self._acknowledgement_text,
            },
            available_at_ms=message.received_at_ms,
        )
        admitted = await self._runtime_store.admit(
            event,
            command,
            acknowledgement=acknowledgement,
            now_ms=message.received_at_ms,
        )
        return IMAdmissionDecision(
            IMAdmissionReason.ADMITTED,
            task_id=admitted.task_id,
            created=admitted.created,
        )

    def _evaluate(self, message: IncomingMessage) -> IMAdmissionReason:
        if message.chat_type != "group":
            return IMAdmissionReason.NOT_GROUP
        if message.sender_type in {"bot", "app", "system"}:
            return IMAdmissionReason.BOT_SENDER
        if not message.explicitly_mentions(self._bot_open_id):
            return IMAdmissionReason.BOT_NOT_MENTIONED
        if not message.body_text.strip():
            return IMAdmissionReason.EMPTY_REQUEST
        return IMAdmissionReason.ADMITTED

    @staticmethod
    def _reply_key(message: IncomingMessage, kind: str) -> str:
        return f"im:{message.tenant_key}:{message.app_id}:{message.message_id}:{kind}:v1"
