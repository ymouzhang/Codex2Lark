from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from codex2lark.core.events import LeasedTask, NormalizedEvent, TaskCommand, TaskState
from codex2lark.core.ids import new_trace_id
from codex2lark.core.models import Identity
from codex2lark.runtime.tasks import TaskExecutionResult
from codex2lark.storage.runtime_store import RuntimeStore


class MembershipService(Protocol):
    async def ensure_current_user(
        self, *, chat_id: str, chat_identity: Identity
    ) -> dict[str, Any]: ...


class ChatAccessRepository(Protocol):
    async def restore_chat_access(
        self, *, tenant_key: str, app_id: str, chat_id: str, now_ms: int
    ) -> None: ...


class BotAddedAdmissionService:
    def __init__(
        self, store: RuntimeStore, *, app_id: str, received_at_ms: Callable[[], int]
    ) -> None:
        if not app_id:
            raise ValueError("app_id is required")
        self._store = store
        self._app_id = app_id
        self._received_at_ms = received_at_ms

    async def handle_bot_added(self, value: object) -> None:
        raw = getattr(value, "raw", None)
        header = raw.get("header") if isinstance(raw, dict) else None
        header = header if isinstance(header, dict) else {}
        event_id = self._required_text(header.get("event_id"), "event_id")
        tenant_key = self._required_text(header.get("tenant_key"), "tenant_key")
        chat_id = self._required_text(getattr(value, "chat_id", None), "chat_id")
        occurred_at_ms = self._timestamp_ms(header.get("create_time"))
        now_ms = self._received_at_ms()
        payload = {"tenant_key": tenant_key, "app_id": self._app_id, "chat_id": chat_id}
        await self._store.admit(
            NormalizedEvent(
                event_id=event_id,
                plugin_id="feishu-im",
                event_type="im.chat.member.bot.added_v1",
                tenant_key=tenant_key,
                app_id=self._app_id,
                occurred_at_ms=occurred_at_ms,
                received_at_ms=now_ms,
                resource_kind="im.chat",
                resource_id=chat_id,
                trace_id=str(new_trace_id()),
                source_payload=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
                payload_expires_at_ms=now_ms + 24 * 60 * 60 * 1000,
            ),
            TaskCommand(
                plugin_id="feishu-im",
                command_type="im.ensure_owner_membership",
                session_key=f"{tenant_key}/{self._app_id}/{chat_id}/membership",
                payload=payload,
                available_at_ms=now_ms,
                group_id=chat_id,
            ),
            now_ms=now_ms,
        )

    @staticmethod
    def _required_text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"bot-added event is missing {field}")
        return value

    @staticmethod
    def _timestamp_ms(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("bot-added event is missing create_time")
        result = int(value)
        return result * 1000 if result < 1_000_000_000_000 else result


class MembershipTaskHandler:
    def __init__(
        self,
        service: MembershipService,
        *,
        bot_identity: Identity,
        access_repository: ChatAccessRepository | None = None,
    ) -> None:
        self._service = service
        self._bot_identity = bot_identity
        self._access_repository = access_repository

    async def execute(self, task: LeasedTask, *, now_ms: int) -> TaskExecutionResult:
        chat_id = task.payload.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("membership task requires chat_id")
        await self._service.ensure_current_user(
            chat_id=chat_id,
            chat_identity=self._bot_identity,
        )
        if self._access_repository is not None:
            tenant_key = task.payload.get("tenant_key")
            app_id = task.payload.get("app_id")
            if not isinstance(tenant_key, str) or not isinstance(app_id, str):
                raise ValueError("membership task requires tenant_key and app_id")
            await self._access_repository.restore_chat_access(
                tenant_key=tenant_key,
                app_id=app_id,
                chat_id=chat_id,
                now_ms=now_ms,
            )
        return TaskExecutionResult(TaskState.SUCCEEDED)

    def failure(self, task: LeasedTask, error: BaseException) -> TaskExecutionResult:
        del task
        return TaskExecutionResult(TaskState.FAILED, error_code=type(error).__name__)
