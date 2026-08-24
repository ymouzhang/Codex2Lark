from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from codex2lark.core.events import NormalizedEvent, OutboxDraft, TaskCommand
from codex2lark.core.ids import new_trace_id
from codex2lark.runtime.controls import RunControlKind
from codex2lark.storage.runtime_store import RuntimeStore

from .models import IMAdmissionDecision, IMAdmissionReason, IncomingMessage


class MessageMirror(Protocol):
    async def upsert_message(self, message: IncomingMessage) -> bool: ...


class IMAdmissionService:
    def __init__(
        self,
        runtime_store: RuntimeStore,
        message_mirror: MessageMirror,
        *,
        bot_open_id: str | Callable[[], str | None],
        acknowledgement_text: str,
        agent_definition_version: int | Callable[[IncomingMessage], int] = 1,
    ) -> None:
        if not bot_open_id:
            raise ValueError("bot_open_id is required")
        if not acknowledgement_text.strip():
            raise ValueError("acknowledgement_text is required")
        self._runtime_store = runtime_store
        self._message_mirror = message_mirror
        self._bot_open_id = (lambda: bot_open_id) if isinstance(bot_open_id, str) else bot_open_id
        self._acknowledgement_text = acknowledgement_text
        self._agent_definition_version = (
            (lambda _message: agent_definition_version)
            if isinstance(agent_definition_version, int)
            else agent_definition_version
        )

    async def admit(self, message: IncomingMessage) -> IMAdmissionDecision:
        reason = self._evaluate(message)
        if reason is not IMAdmissionReason.ADMITTED:
            return IMAdmissionDecision(reason)

        if not await self._message_mirror.upsert_message(message):
            return IMAdmissionDecision(IMAdmissionReason.ACCESS_REVOKED)
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
                "root_id": message.root_id,
                "sender_id": message.sender_id,
                "request": message.body_text.strip(),
                "agent_definition_version": self._selected_version(message),
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
        control_kind, control_text = self._classify_control(message.body_text)
        controlled = await self._runtime_store.admit_control(
            event,
            session_key=message.session_key,
            actor_id=message.sender_id,
            kind=control_kind,
            text=control_text,
            acknowledgement=acknowledgement,
            now_ms=message.received_at_ms,
        )
        if controlled is not None:
            return IMAdmissionDecision(
                IMAdmissionReason.ADMITTED,
                task_id=controlled.task_id,
                created=controlled.created,
                control_id=controlled.control_id,
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

    def _selected_version(self, message: IncomingMessage) -> int:
        version = self._agent_definition_version(message)
        if version < 1:
            raise ValueError("selected Agent definition version must be positive")
        return version

    @staticmethod
    def _classify_control(body: str) -> tuple[RunControlKind, str]:
        text = body.strip()
        normalized = text.casefold()
        if normalized in {"/cancel", "取消任务", "停止任务"}:
            return RunControlKind.CANCEL, text
        if normalized in {"/interrupt", "暂停任务"}:
            return RunControlKind.INTERRUPT, text
        prefixes = (
            "/steer ",
            "更正：",  # noqa: RUF001 - intentional user syntax
            "更正:",
            "调整：",  # noqa: RUF001 - intentional user syntax
            "调整:",
            "改为：",  # noqa: RUF001 - intentional user syntax
            "改为:",
        )
        for prefix in prefixes:
            if normalized.startswith(prefix.casefold()):
                instruction = text[len(prefix) :].strip()
                if instruction:
                    return RunControlKind.STEER, instruction
        return RunControlKind.FOLLOW_UP, text

    def _evaluate(self, message: IncomingMessage) -> IMAdmissionReason:
        if message.chat_type != "group":
            return IMAdmissionReason.NOT_GROUP
        if message.sender_type in {"bot", "app", "system"}:
            return IMAdmissionReason.BOT_SENDER
        bot_open_id = self._bot_open_id()
        if not bot_open_id or not message.explicitly_mentions(bot_open_id):
            return IMAdmissionReason.BOT_NOT_MENTIONED
        if not message.body_text.strip():
            return IMAdmissionReason.EMPTY_REQUEST
        return IMAdmissionReason.ADMITTED

    @staticmethod
    def _reply_key(message: IncomingMessage, kind: str) -> str:
        return f"im:{message.tenant_key}:{message.app_id}:{message.message_id}:{kind}:v1"
