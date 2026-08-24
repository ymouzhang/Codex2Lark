from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codex2lark.capabilities.im.admission import IMAdmissionService
from codex2lark.capabilities.im.channel_adapter import (
    ChannelMessageNormalizer,
    OfficialChannelEventSource,
)
from codex2lark.capabilities.im.models import (
    AttachmentReference,
    IMAdmissionReason,
    IncomingMessage,
    Mention,
)
from codex2lark.capabilities.im.plugin import create_plugin
from codex2lark.capabilities.im.publisher import IMOutboxPublisher
from codex2lark.capabilities.im.repository import SQLiteIMRepository
from codex2lark.core.events import LeasedOutboxMessage
from codex2lark.runtime.outbox import OutboxDispatcher
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.runtime_store import RuntimeStore


def message(**changes: object) -> IncomingMessage:
    value = IncomingMessage(
        event_id="event-1",
        tenant_key="tenant-1",
        app_id="app-1",
        chat_id="oc_group",
        chat_type="group",
        chat_name="Project room",
        message_id="om_request",
        message_type="text",
        sender_id="ou_user",
        sender_type="user",
        sender_name="Aaron",
        body_text="Please produce the architecture document.",
        mentions=(Mention("ou_bot", "Codex2Lark"),),
        attachments=(
            AttachmentReference("file-key", "file", "private-plan.docx", "application/docx", 12),
        ),
        occurred_at_ms=100,
        received_at_ms=110,
        thread_id="omt_thread",
    )
    return replace(value, **changes)


async def setup(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    cipher = EnvelopeCipher(MasterKey("test", b"i" * 32))
    repository = SQLiteIMRepository(database, cipher)
    runtime_store = RuntimeStore(database, cipher)
    service = IMAdmissionService(
        runtime_store,
        repository,
        bot_open_id="ou_bot",
        acknowledgement_text="I will take care of this and report back when it is done.",
    )
    return database, repository, runtime_store, service


async def test_im_plugin_manifest_and_lifecycle() -> None:
    plugin = create_plugin()
    assert plugin.manifest.storage_namespace == "im"
    assert "im.message.receive_v1" in plugin.manifest.events
    assert not (await plugin.health()).healthy
    await plugin.initialize()
    assert (await plugin.health()).healthy
    await plugin.stop()


async def test_exact_mention_admission_rejects_non_requests(tmp_path: Path) -> None:
    database, _repository, _runtime_store, service = await setup(tmp_path)
    try:
        cases = (
            (replace(message(), chat_type="p2p"), IMAdmissionReason.NOT_GROUP),
            (replace(message(), sender_type="bot"), IMAdmissionReason.BOT_SENDER),
            (replace(message(), mentions=()), IMAdmissionReason.BOT_NOT_MENTIONED),
            (replace(message(), body_text="  "), IMAdmissionReason.EMPTY_REQUEST),
        )
        for incoming, reason in cases:
            assert (await service.admit(incoming)).reason is reason
        counts = await database.call(
            lambda connection: tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("im_messages", "runtime_tasks", "runtime_outbox")
            )
        )
        assert counts == (0, 0, 0)
    finally:
        await database.close()


async def test_admission_mirrors_encrypts_and_deduplicates_message(tmp_path: Path) -> None:
    database, repository, _runtime_store, service = await setup(tmp_path)
    try:
        first = await service.admit(message())
        duplicate = await service.admit(message())

        assert first.reason is IMAdmissionReason.ADMITTED
        assert first.created
        assert duplicate.task_id == first.task_id
        assert not duplicate.created
        stored = await repository.get_message("tenant-1", "app-1", "om_request")
        assert stored is not None
        assert stored.body_text.startswith("Please produce")
        assert stored.mentions == (Mention("ou_bot", "Codex2Lark"),)
        rows = await database.call(
            lambda connection: (
                connection.execute("SELECT COUNT(*) FROM runtime_tasks").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0],
                connection.execute("SELECT content_ciphertext FROM im_messages").fetchone()[0],
                connection.execute("SELECT filename_ciphertext FROM im_attachments").fetchone()[0],
            )
        )
        assert rows[:2] == (1, 1)
        assert b"Please produce" not in rows[2]
        assert b"private-plan.docx" not in rows[3]
    finally:
        await database.close()


async def test_newer_tombstone_wins_and_recent_context_is_chronological(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    try:
        await repository.upsert_message(message(message_id="om_1", body_text="one"))
        await repository.upsert_message(
            message(
                event_id="event-2",
                message_id="om_2",
                body_text="two",
                occurred_at_ms=200,
                received_at_ms=210,
            )
        )
        await repository.upsert_message(
            message(
                event_id="old",
                message_id="om_2",
                body_text="stale",
                occurred_at_ms=150,
                received_at_ms=220,
            )
        )
        await repository.upsert_message(
            message(
                event_id="recall",
                message_id="om_2",
                body_text="",
                occurred_at_ms=200,
                updated_at_ms=300,
                received_at_ms=310,
                is_recalled=True,
            )
        )

        context = await repository.recent_messages("tenant-1", "app-1", "oc_group", before_ms=400)
        assert [item.message_id for item in context] == ["om_1"]
        recalled = await repository.get_message("tenant-1", "app-1", "om_2")
        assert recalled is not None and recalled.is_recalled
    finally:
        await database.close()


def channel_message() -> SimpleNamespace:
    return SimpleNamespace(
        raw={
            "header": {"event_id": "event-channel", "tenant_key": "tenant-1"},
            "event": {
                "sender": {"sender_type": "user"},
                "message": {
                    "message_type": "text",
                    "content": '{"text":"@_user_1 please create the document"}',
                    "mentions": [
                        {
                            "key": "@_user_1",
                            "id": {"open_id": "ou_bot"},
                            "name": "Codex2Lark",
                        }
                    ],
                    "root_id": "om_root",
                    "parent_id": "om_parent",
                },
            },
        },
        message_id="om_channel",
        chat_id="oc_group",
        chat_type="topic",
        conversation=SimpleNamespace(chat_type="topic", thread_id="omt_thread"),
        sender_id="ou_user",
        sender_name="Aaron",
        sender=SimpleNamespace(is_bot=False),
        create_time="1720000000",
        raw_content_type="text",
        content_text="@Codex2Lark please create the document",
        safe_content_text="@Codex2Lark please create the document",
        mentions=[],
        resources=[SimpleNamespace(type="file", file_key="file-key", file_name="plan.docx")],
    )


def test_channel_normalizer_uses_raw_explicit_mentions_and_strips_bot_placeholder() -> None:
    normalized = ChannelMessageNormalizer(app_id="app-1", bot_open_id="ou_bot").normalize(
        channel_message(), received_at_ms=200
    )

    assert normalized.chat_type == "group"
    assert normalized.body_text == "please create the document"
    assert normalized.mentions == (Mention("ou_bot", "Codex2Lark"),)
    assert normalized.explicitly_mentions("ou_bot")
    assert normalized.occurred_at_ms == 1_720_000_000_000
    assert normalized.attachments[0].filename == "plan.docx"


class FakeAdmission:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []

    async def admit(self, incoming: IncomingMessage) -> object:
        self.messages.append(incoming)
        return object()


class FakeChannel:
    def __init__(self) -> None:
        self.bot_identity = SimpleNamespace(open_id="ou_bot")
        self.handlers: dict[str, Any] = {}
        self.connected = False
        self.sent: list[tuple[str, dict[str, str], dict[str, object]]] = []
        self.send_result: object = SimpleNamespace(success=True, message_id="om_reply")

    def on(self, event: str, handler: Any) -> object:
        self.handlers[event] = handler
        return object()

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None:
        assert timeout == 30.0
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send(self, to: str, outbound: dict[str, str], opts: dict[str, object]) -> object:
        self.sent.append((to, outbound, opts))
        return self.send_result


async def test_official_channel_source_queues_and_normalizes_callbacks() -> None:
    channel = FakeChannel()
    admission = FakeAdmission()
    source = OfficialChannelEventSource(
        channel,
        admission,
        app_id="app-1",
        received_at_ms=lambda: 500,
        capacity=1,
    )
    await source.start()
    try:
        await channel.handlers["message"](channel_message())
        for _ in range(100):
            if admission.messages:
                break
            await asyncio.sleep(0)
        assert admission.messages[0].received_at_ms == 500
    finally:
        await source.stop()


async def test_im_outbox_publisher_preserves_thread_and_requires_confirmation() -> None:
    channel = FakeChannel()
    publisher = IMOutboxPublisher(channel)
    item = LeasedOutboxMessage(
        outbox_id="outbox-1",
        run_id=None,
        publisher_id="feishu-im.reply",
        destination_ref="om_channel",
        message_kind="acknowledgement",
        idempotency_key="stable-key",
        payload={
            "chat_id": "oc_group",
            "message_id": "om_channel",
            "reply_in_thread": True,
            "text": "I am working on this.",
        },
        attempt_count=1,
        lease_expires_at_ms=1000,
    )

    assert await publisher.publish(item) == "om_reply"
    assert channel.sent[0][2] == {
        "reply_to": "om_channel",
        "reply_in_thread": True,
        "receive_id_type": "chat_id",
        "reply_target_gone": "fail",
        "uuid": "stable-key",
    }
    channel.send_result = SimpleNamespace(success=True, message_id=None)
    try:
        await publisher.publish(item)
    except RuntimeError as exc:
        assert "not confirmed" in str(exc)
    else:
        raise AssertionError("ambiguous send must remain retryable")


async def test_outbox_dispatcher_retries_ambiguous_reply_then_marks_sent(
    tmp_path: Path,
) -> None:
    database, repository, runtime_store, service = await setup(tmp_path)
    try:
        await service.admit(message())
        channel = FakeChannel()
        channel.send_result = SimpleNamespace(success=True, message_id=None)
        dispatcher = OutboxDispatcher(
            runtime_store,
            {"feishu-im.reply": IMOutboxPublisher(channel)},
            worker_id="outbox-worker",
            retry_delay_ms=10,
        )

        failed = await dispatcher.run_once(now_ms=110)
        assert len(failed.retry_ids) == 1
        channel.send_result = SimpleNamespace(success=True, message_id="om_confirmed")
        sent = await dispatcher.run_once(now_ms=120)
        assert sent.sent_ids == failed.retry_ids
        state = await database.call(
            lambda connection: connection.execute(
                "SELECT state, upstream_ref FROM runtime_outbox"
            ).fetchone()
        )
        assert tuple(state) == ("sent", "om_confirmed")
        assert await repository.get_message("tenant-1", "app-1", "om_request") is not None
    finally:
        await database.close()
