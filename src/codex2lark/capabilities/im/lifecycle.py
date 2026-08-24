from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from codex2lark.core.events import LeasedTask, NormalizedEvent, TaskCommand, TaskState
from codex2lark.core.ids import new_trace_id
from codex2lark.runtime.tasks import TaskExecutionResult
from codex2lark.storage.runtime_store import RuntimeStore


class IMLifecycleRepository(Protocol):
    async def invalidate_message(
        self,
        *,
        tenant_key: str,
        app_id: str,
        chat_id: str,
        message_id: str,
        source_version_ms: int,
        now_ms: int,
    ) -> tuple[str, ...]: ...

    async def revoke_chat_access(
        self, *, tenant_key: str, app_id: str, chat_id: str, now_ms: int
    ) -> tuple[str, ...]: ...


class BlobCleaner(Protocol):
    def delete(self, blob_id: str) -> bool: ...


class IMLifecycleAdmissionService:
    def __init__(
        self, store: RuntimeStore, *, app_id: str, received_at_ms: Callable[[], int]
    ) -> None:
        if not app_id:
            raise ValueError("app_id is required")
        self._store = store
        self._app_id = app_id
        self._received_at_ms = received_at_ms

    async def handle_message_recalled(self, raw: dict[str, Any]) -> None:
        header, event = self._envelope(raw)
        message_id = self._required_text(event.get("message_id"), "message_id")
        chat_id = self._required_text(event.get("chat_id"), "chat_id")
        source_version_ms = self._timestamp_ms(
            event.get("recall_time") or header.get("create_time"), "recall_time"
        )
        await self._admit(
            header,
            event_type="im.message.recalled_v1",
            resource_kind="im.message",
            resource_id=message_id,
            command_type="im.invalidate_message",
            session_suffix=f"{chat_id}/lifecycle/{message_id}",
            payload={
                "tenant_key": self._required_text(header.get("tenant_key"), "tenant_key"),
                "app_id": self._app_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "source_version_ms": source_version_ms,
            },
        )

    async def handle_bot_removed(self, raw: dict[str, Any]) -> None:
        header, event = self._envelope(raw)
        chat_id = self._required_text(event.get("chat_id"), "chat_id")
        tenant_key = self._required_text(header.get("tenant_key"), "tenant_key")
        await self._admit(
            header,
            event_type="im.chat.member.bot.deleted_v1",
            resource_kind="im.chat",
            resource_id=chat_id,
            command_type="im.revoke_chat_access",
            session_suffix=f"{chat_id}/lifecycle/access",
            payload={"tenant_key": tenant_key, "app_id": self._app_id, "chat_id": chat_id},
        )

    async def _admit(
        self,
        header: dict[str, Any],
        *,
        event_type: str,
        resource_kind: str,
        resource_id: str,
        command_type: str,
        session_suffix: str,
        payload: dict[str, object],
    ) -> None:
        tenant_key = self._required_text(header.get("tenant_key"), "tenant_key")
        now_ms = self._received_at_ms()
        occurred_at_ms = self._timestamp_ms(header.get("create_time"), "create_time")
        source_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        await self._store.admit(
            NormalizedEvent(
                event_id=self._required_text(header.get("event_id"), "event_id"),
                plugin_id="feishu-im",
                event_type=event_type,
                tenant_key=tenant_key,
                app_id=self._app_id,
                occurred_at_ms=occurred_at_ms,
                received_at_ms=now_ms,
                resource_kind=resource_kind,
                resource_id=resource_id,
                trace_id=str(new_trace_id()),
                source_payload=source_payload,
                payload_expires_at_ms=now_ms + 24 * 60 * 60 * 1000,
            ),
            TaskCommand(
                plugin_id="feishu-im",
                command_type=command_type,
                session_key=f"{tenant_key}/{self._app_id}/{session_suffix}",
                payload=payload,
                available_at_ms=now_ms,
                group_id=(
                    str(payload["chat_id"]) if isinstance(payload.get("chat_id"), str) else None
                ),
            ),
            now_ms=now_ms,
        )

    @staticmethod
    def _envelope(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        header = raw.get("header")
        event = raw.get("event")
        if not isinstance(header, dict) or not isinstance(event, dict):
            raise ValueError("lifecycle event envelope is malformed")
        return header, event

    @staticmethod
    def _required_text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"lifecycle event is missing {field}")
        return value

    @staticmethod
    def _timestamp_ms(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"lifecycle event is missing {field}")
        result = int(value)
        return result * 1000 if result < 1_000_000_000_000 else result


class IMLifecycleTaskHandler:
    def __init__(self, repository: IMLifecycleRepository, blobs: BlobCleaner) -> None:
        self._repository = repository
        self._blobs = blobs

    async def execute(self, task: LeasedTask, *, now_ms: int) -> TaskExecutionResult:
        tenant_key = self._field(task, "tenant_key")
        app_id = self._field(task, "app_id")
        chat_id = self._field(task, "chat_id")
        if task.command_type == "im.invalidate_message":
            raw_version = task.payload.get("source_version_ms")
            if isinstance(raw_version, bool) or not isinstance(raw_version, int):
                raise ValueError("message invalidation requires source_version_ms")
            blob_ids = await self._repository.invalidate_message(
                tenant_key=tenant_key,
                app_id=app_id,
                chat_id=chat_id,
                message_id=self._field(task, "message_id"),
                source_version_ms=raw_version,
                now_ms=now_ms,
            )
        elif task.command_type == "im.revoke_chat_access":
            blob_ids = await self._repository.revoke_chat_access(
                tenant_key=tenant_key, app_id=app_id, chat_id=chat_id, now_ms=now_ms
            )
        else:
            raise ValueError(f"unsupported IM lifecycle command: {task.command_type}")
        for blob_id in blob_ids:
            self._blobs.delete(blob_id)
        return TaskExecutionResult(TaskState.SUCCEEDED)

    def failure(self, task: LeasedTask, error: BaseException) -> TaskExecutionResult:
        del task
        return TaskExecutionResult(TaskState.FAILED, error_code=type(error).__name__)

    @staticmethod
    def _field(task: LeasedTask, name: str) -> str:
        value = task.payload.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"IM lifecycle task requires {name}")
        return value
