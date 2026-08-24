from __future__ import annotations

from pathlib import Path

import pytest

from codex2lark.core.events import NormalizedEvent, TaskCommand
from codex2lark.runtime.approvals import ApprovalDecisionService, DurableApprovalBroker
from codex2lark.runtime.tasks import TaskDeferred
from codex2lark.runtime.tools import ToolContext
from codex2lark.runtime.types import ToolCall, ToolDefinition, ToolEffect
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.runtime_store import RuntimeStore


def incoming(event_id: str = "event-1") -> NormalizedEvent:
    return NormalizedEvent(
        event_id,
        "feishu-im",
        "im.message.receive_v1",
        "tenant-1",
        "app-1",
        1,
        1,
        "im.message",
        "message-1",
        event_id,
    )


async def setup(tmp_path: Path) -> tuple[SQLiteDatabase, RuntimeStore, str]:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = RuntimeStore(database, EnvelopeCipher(MasterKey("test", b"a" * 32)))
    admitted = await store.admit(
        incoming(),
        TaskCommand(
            "feishu-im",
            "im.handle_mention",
            "tenant-1/app-1/chat-1/root-1",
            {"message_id": "message-1"},
        ),
        now_ms=1,
    )
    return database, store, admitted.task_id


async def test_destructive_approval_is_stable_private_and_requester_bound(
    tmp_path: Path,
) -> None:
    database, store, task_id = await setup(tmp_path)
    broker = DurableApprovalBroker(store, clock_ms=lambda: 100, poll_delay_ms=50)
    definition = ToolDefinition(
        "feishu.test.delete", 1, "delete a resource", {}, ToolEffect.DESTRUCTIVE
    )
    context = ToolContext(
        "run-1",
        "/root",
        "tenant-1",
        "app-1",
        "requester-1",
        "tenant-1/app-1/chat-1/root-1",
        "bot:app-1",
        1,
        task_id=task_id,
        chat_id="chat-1",
        source_message_id="message-1",
    )
    secret = "private-resource-token"
    first = ToolCall("call-1", definition.tool_id, {"resource": secret})
    second = ToolCall("call-2", definition.tool_id, {"resource": secret})
    try:
        with pytest.raises(TaskDeferred, match="approval_pending"):
            await broker.request(definition, first, context)
        with pytest.raises(TaskDeferred, match="approval_pending"):
            await broker.request(definition, second, context)

        outbox = await store.lease_outbox(worker_id="outbox", now_ms=100, lease_ms=100, limit=10)
        assert len(outbox) == 1
        assert outbox[0].message_kind == "approval"
        assert secret not in str(outbox[0].payload)
        approval_id = DurableApprovalBroker.approval_id(
            "run-1", definition.tool_id, broker_digest(first.arguments)
        )

        unauthorized = await store.decide_approval(
            approval_event("click-other", actor_id="other"),
            approval_id=approval_id,
            actor_id="other",
            decision="approved",
            acknowledgement=acknowledgement,
            now_ms=110,
        )
        assert unauthorized == "unauthorized"
        assert await approval_state(database, approval_id) == "pending"

        approved = await ApprovalDecisionService(
            store, app_id="app-1", received_at_ms=lambda: 120
        ).handle(
            {
                "header": {"event_id": "click-owner"},
                "event": {
                    "operator": {
                        "tenant_key": "tenant-1",
                        "open_id": "requester-1",
                    },
                    "action": {
                        "value": {
                            "approval_id": approval_id,
                            "decision": "approved",
                        }
                    },
                    "context": {
                        "open_message_id": "approval-message",
                        "open_chat_id": "chat-1",
                    },
                },
            }
        )
        assert approved == "approved"
        assert await broker.request(definition, second, context)
    finally:
        await database.close()


def broker_digest(arguments: dict[str, object]) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def approval_event(event_id: str, *, actor_id: str) -> NormalizedEvent:
    return NormalizedEvent(
        event_id,
        "feishu-im",
        "card.approval.decided",
        "tenant-1",
        "app-1",
        100,
        100,
        "im.message",
        "approval-message",
        event_id,
        source_payload=f"actor={actor_id}".encode(),
    )


def acknowledgement(result: str):
    from codex2lark.core.events import OutboxDraft

    return OutboxDraft(
        "feishu-im.reply",
        "approval-message",
        "progress",
        f"approval-result:{result}",
        {"chat_id": "chat-1", "message_id": "approval-message", "text": result},
    )


async def approval_state(database: SQLiteDatabase, approval_id: str) -> str:
    return str(
        await database.call(
            lambda connection: connection.execute(
                "SELECT state FROM runtime_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()[0]
        )
    )
