from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from codex2lark.capabilities.im.admission import IMAdmissionService
from codex2lark.capabilities.im.models import (
    AttachmentReference,
    IMAdmissionReason,
    IncomingMessage,
    Mention,
)
from codex2lark.capabilities.im.plugin import create_plugin
from codex2lark.capabilities.im.repository import SQLiteIMRepository
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
    service = IMAdmissionService(
        RuntimeStore(database, cipher),
        repository,
        bot_open_id="ou_bot",
        acknowledgement_text="I will take care of this and report back when it is done.",
    )
    return database, repository, service


async def test_im_plugin_manifest_and_lifecycle() -> None:
    plugin = create_plugin()
    assert plugin.manifest.storage_namespace == "im"
    assert "im.message.receive_v1" in plugin.manifest.events
    assert not (await plugin.health()).healthy
    await plugin.initialize()
    assert (await plugin.health()).healthy
    await plugin.stop()


async def test_exact_mention_admission_rejects_non_requests(tmp_path: Path) -> None:
    database, _repository, service = await setup(tmp_path)
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
    database, repository, service = await setup(tmp_path)
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
    database, repository, _service = await setup(tmp_path)
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
