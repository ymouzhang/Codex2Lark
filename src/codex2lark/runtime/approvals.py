from __future__ import annotations

# ruff: noqa: RUF001
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any, ClassVar, Protocol

from codex2lark.core.events import NormalizedEvent, OutboxDraft

from .tasks import TaskDeferred
from .tools import ToolContext
from .types import ToolCall, ToolDefinition


class ApprovalStore(Protocol):
    async def request_approval(
        self,
        *,
        approval_id: str,
        task_id: str,
        run_id: str,
        tenant_key: str,
        app_id: str,
        session_key: str,
        actor_id: str,
        tool_id: str,
        argument_digest: str,
        trace_id: str,
        expires_at_ms: int,
        card: OutboxDraft,
        now_ms: int,
    ) -> str: ...

    async def decide_approval(
        self,
        event: NormalizedEvent,
        *,
        approval_id: str,
        actor_id: str,
        decision: str,
        acknowledgement: Callable[[str], OutboxDraft],
        now_ms: int,
    ) -> str: ...


class DurableApprovalBroker:
    def __init__(
        self,
        store: ApprovalStore,
        *,
        ttl_ms: int = 15 * 60 * 1000,
        poll_delay_ms: int = 2_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if min(ttl_ms, poll_delay_ms) < 1:
            raise ValueError("approval timing must be positive")
        self._store = store
        self._ttl_ms = ttl_ms
        self._poll_delay_ms = poll_delay_ms
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    async def request(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> bool:
        if context.task_id is None or context.chat_id is None or context.source_message_id is None:
            raise RuntimeError("destructive approval requires a durable IM task binding")
        canonical = json.dumps(
            call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        argument_digest = hashlib.sha256(canonical).hexdigest()
        approval_id = self.approval_id(context.run_id, definition.tool_id, argument_digest)
        now_ms = self._clock_ms()
        state = await self._store.request_approval(
            approval_id=approval_id,
            task_id=context.task_id,
            run_id=context.run_id,
            tenant_key=context.tenant_key,
            app_id=context.app_id,
            session_key=context.session_key,
            actor_id=context.actor_id,
            tool_id=definition.tool_id,
            argument_digest=argument_digest,
            trace_id=context.trace_id or context.run_id,
            expires_at_ms=now_ms + self._ttl_ms,
            card=self._card(definition, context, approval_id, now_ms + self._ttl_ms),
            now_ms=now_ms,
        )
        if state == "approved":
            return True
        if state == "pending":
            raise TaskDeferred("approval_pending", delay_ms=self._poll_delay_ms)
        return False

    @staticmethod
    def approval_id(run_id: str, tool_id: str, argument_digest: str) -> str:
        value = hashlib.sha256(f"{run_id}\0{tool_id}\0{argument_digest}".encode()).hexdigest()
        return f"apr_{value}"

    @staticmethod
    def _card(
        definition: ToolDefinition,
        context: ToolContext,
        approval_id: str,
        expires_at_ms: int,
    ) -> OutboxDraft:
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "需要你的确认"}},
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            "即将执行一项可能删除或覆盖数据的操作。\n"
                            f"**操作类型：** `{definition.tool_id}`\n"
                            "**目标：** 当前请求绑定的飞书资源\n"
                            "请确认是否继续。为保护隐私，卡片不会展示原始参数。"
                        ),
                    },
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "确认执行"},
                                "type": "primary",
                                "value": {"approval_id": approval_id, "decision": "approved"},
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "暂不执行"},
                                "type": "default",
                                "value": {"approval_id": approval_id, "decision": "rejected"},
                            },
                        ],
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": f"审批将在 {expires_at_ms}（Unix ms）后失效。",
                            }
                        ],
                    },
                ]
            },
        }
        return OutboxDraft(
            publisher_id="feishu-im.reply",
            destination_ref=context.source_message_id or "",
            message_kind="approval",
            idempotency_key=f"approval:{approval_id}:request:v1",
            payload={
                "chat_id": context.chat_id,
                "message_id": context.source_message_id,
                "reply_in_thread": context.reply_in_thread,
                "card": card,
            },
        )


class ApprovalDecisionService:
    _messages: ClassVar[dict[str, str]] = {
        "approved": "好的，已经收到你的确认，我继续处理喔～",
        "rejected": "好的，这项操作不会执行。如果想换个方式处理，告诉我就好。",
        "expired": "这次确认已经过期了，请重新发起请求，我再帮你处理。",
        "unauthorized": "这张确认卡只能由最初提出请求的人操作喔。",
    }

    def __init__(
        self,
        store: ApprovalStore,
        *,
        app_id: str,
        received_at_ms: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._app_id = app_id
        self._received_at_ms = received_at_ms or (lambda: int(time.time() * 1000))

    async def handle(self, raw: dict[str, Any]) -> str:
        header = self._mapping(raw.get("header"))
        event = self._mapping(raw.get("event"))
        action = self._mapping(event.get("action"))
        value = self._mapping(action.get("value"))
        operator = self._mapping(event.get("operator"))
        callback_context = self._mapping(event.get("context"))
        approval_id = self._text(value.get("approval_id"), "approval_id")
        decision = self._text(value.get("decision"), "decision")
        actor_id = self._text(operator.get("open_id"), "operator.open_id")
        tenant_key = self._text(operator.get("tenant_key"), "operator.tenant_key")
        message_id = self._text(callback_context.get("open_message_id"), "message_id")
        chat_id = self._text(callback_context.get("open_chat_id"), "chat_id")
        event_id = self._text(header.get("event_id"), "header.event_id")
        now_ms = self._received_at_ms()
        normalized = NormalizedEvent(
            event_id=event_id,
            plugin_id="feishu-im",
            event_type="card.approval.decided",
            tenant_key=tenant_key,
            app_id=self._app_id,
            occurred_at_ms=now_ms,
            received_at_ms=now_ms,
            resource_kind="im.message",
            resource_id=message_id,
            trace_id=event_id,
            source_payload=json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

        def acknowledgement(result: str) -> OutboxDraft:
            return OutboxDraft(
                publisher_id="feishu-im.reply",
                destination_ref=message_id,
                message_kind="progress",
                idempotency_key=f"approval:{approval_id}:decision:{event_id}",
                payload={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_in_thread": False,
                    "text": self._messages.get(result, "这次确认已经处理。"),
                },
            )

        return await self._store.decide_approval(
            normalized,
            approval_id=approval_id,
            actor_id=actor_id,
            decision=decision,
            acknowledgement=acknowledgement,
            now_ms=now_ms,
        )

    @staticmethod
    def _mapping(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("approval callback field is not an object")
        return value

    @staticmethod
    def _text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"approval callback requires {field}")
        return value
