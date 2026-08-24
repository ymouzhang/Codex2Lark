from __future__ import annotations

import asyncio
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codex2lark.capabilities.im.admission import IMAdmissionService
from codex2lark.capabilities.im.attachments import (
    AttachmentEvidence,
    AttachmentLoadRequest,
    AttachmentService,
    SafeAttachmentParser,
)
from codex2lark.capabilities.im.channel_adapter import (
    ChannelMessageNormalizer,
    OfficialChannelEventSource,
)
from codex2lark.capabilities.im.context_provider import (
    IMContextProvider,
    IMContextRequest,
    MessagePage,
)
from codex2lark.capabilities.im.membership import (
    BotAddedAdmissionService,
    MembershipTaskHandler,
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
from codex2lark.capabilities.im.task_handler import (
    IMMentionTaskHandler,
    IMResponseTemplates,
)
from codex2lark.core.events import LeasedOutboxMessage, LeasedTask
from codex2lark.core.models import Identity
from codex2lark.runtime.context import ContextEvidence
from codex2lark.runtime.outbox import OutboxDispatcher
from codex2lark.runtime.sessions import InMemorySessionStore
from codex2lark.runtime.types import AgentDefinition, AgentOutcome, RunStatus
from codex2lark.storage.blobs import EncryptedBlobStore
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


class FakeBotAddedHandler:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def handle_bot_added(self, event: object) -> None:
        self.events.append(event)


async def test_official_channel_source_queues_bot_added_callbacks() -> None:
    channel = FakeChannel()
    membership = FakeBotAddedHandler()
    source = OfficialChannelEventSource(
        channel,
        FakeAdmission(),
        app_id="app-1",
        received_at_ms=lambda: 500,
        bot_added_handler=membership,
        capacity=1,
    )
    event = SimpleNamespace(chat_id="oc_group")
    await source.start()
    try:
        await channel.handlers["botAdded"](event)
        for _ in range(100):
            if membership.events:
                break
            await asyncio.sleep(0)
        assert membership.events == [event]
    finally:
        await source.stop()


class FakeMembershipService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Identity]] = []

    async def ensure_current_user(
        self, *, chat_id: str, chat_identity: Identity
    ) -> dict[str, object]:
        self.calls.append((chat_id, chat_identity))
        return {"status": "added"}


async def test_bot_added_event_is_durable_replay_safe_and_executes_membership(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    runtime = RuntimeStore(database, EnvelopeCipher(MasterKey("test", b"m" * 32)))
    admission = BotAddedAdmissionService(
        runtime,
        app_id="app-1",
        received_at_ms=lambda: 1_720_000_000_000,
    )
    event = SimpleNamespace(
        chat_id="oc_group",
        raw={
            "header": {
                "event_id": "event-bot-added",
                "tenant_key": "tenant-1",
                "create_time": "1720000000",
            }
        },
    )
    try:
        await admission.handle_bot_added(event)
        await admission.handle_bot_added(event)
        counts = await runtime.counts()
        assert counts["runtime_tasks"] == 1
        task = (
            await runtime.lease_tasks(worker_id="worker", now_ms=1_720_000_000_000, lease_ms=100)
        )[0]
        service = FakeMembershipService()
        handler = MembershipTaskHandler(service, bot_identity=Identity.BOT)

        result = await handler.execute(task, now_ms=1_720_000_000_000)

        assert result.state.value == "succeeded"
        assert service.calls == [("oc_group", Identity.BOT)]
    finally:
        await database.close()


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


class FakeLiveIMReader:
    def __init__(
        self,
        trigger: IncomingMessage,
        related: tuple[IncomingMessage, ...],
        *,
        complete: bool = True,
    ) -> None:
        self.trigger = trigger
        self.related = related
        self.complete = complete
        self.used_related = False

    async def get_message(self, request: IMContextRequest) -> IncomingMessage:
        assert request.message_id == self.trigger.message_id
        return self.trigger

    async def related_messages(self, trigger: IncomingMessage, *, limit: int) -> MessagePage:
        assert trigger == self.trigger and limit == 50
        self.used_related = True
        return MessagePage(self.related, self.complete)

    async def recent_messages(
        self, trigger: IncomingMessage, *, since_ms: int, limit: int
    ) -> MessagePage:
        assert trigger == self.trigger and since_ms >= 0 and limit == 30
        return MessagePage(self.related, self.complete)


async def test_context_provider_refetches_orders_versions_and_marks_incomplete(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    trigger = message()
    older = message(
        event_id="older",
        message_id="om_older",
        body_text="earlier context",
        occurred_at_ms=10,
        received_at_ms=20,
    )
    newer = message(
        event_id="newer",
        message_id="om_newer",
        body_text="later context",
        occurred_at_ms=50,
        received_at_ms=60,
    )
    recalled = message(
        event_id="recalled",
        message_id="om_recalled",
        body_text="removed",
        occurred_at_ms=30,
        received_at_ms=40,
        is_recalled=True,
    )
    source = FakeLiveIMReader(
        trigger,
        (newer, recalled, older, newer, trigger),
        complete=False,
    )
    provider = IMContextProvider(source, repository)
    try:
        bundle = await provider.collect(
            IMContextRequest("tenant-1", "app-1", "oc_group", "om_request")
        )
        assert source.used_related
        assert [item.source_ref for item in bundle.evidence] == [
            "im.message:om_older",
            "im.message:om_newer",
        ]
        assert bundle.warnings == ("im_context_incomplete",)
        assert bundle.evidence[0].source_version == "10"
        mirrored = await repository.get_message("tenant-1", "app-1", "om_recalled")
        assert mirrored is not None and mirrored.is_recalled
    finally:
        await database.close()


async def test_context_provider_rejects_cross_chat_source_data(tmp_path: Path) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    trigger = message()
    source = FakeLiveIMReader(
        trigger,
        (message(message_id="om_other", chat_id="oc_other"),),
    )
    try:
        provider = IMContextProvider(source, repository)
        try:
            await provider.collect(IMContextRequest("tenant-1", "app-1", "oc_group", "om_request"))
        except PermissionError as exc:
            assert "trusted binding" in str(exc)
        else:
            raise AssertionError("cross-chat context must be rejected")
    finally:
        await database.close()


class FakeAttachmentLoader:
    def __init__(self) -> None:
        self.requests: list[tuple[object, int]] = []

    async def load(self, request: object, *, now_ms: int) -> object:
        self.requests.append((request, now_ms))
        return AttachmentEvidence(
            "blob-1",
            "document",
            ContextEvidence(
                "im.attachment:om_request:file-key",
                "Parsed attachment facts",
                "blob-1",
            ),
            "parser_output_truncated",
        )


async def test_context_provider_loads_bound_attachment_evidence_and_warning(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    trigger = message()
    loader = FakeAttachmentLoader()
    provider = IMContextProvider(
        FakeLiveIMReader(trigger, ()),
        repository,
        attachments=loader,
        clock_ms=lambda: 500,
    )
    try:
        bundle = await provider.collect(
            IMContextRequest("tenant-1", "app-1", "oc_group", "om_request")
        )

        assert bundle.evidence[-1].content == "Parsed attachment facts"
        assert bundle.warnings == ("parser_output_truncated",)
        request, now_ms = loader.requests[0]
        assert isinstance(request, AttachmentLoadRequest)
        assert request.message_id == "om_request"
        assert now_ms == 500
    finally:
        await database.close()


class FakeDownloader:
    def __init__(self, content: bytes | None) -> None:
        self.content = content
        self.calls = 0

    async def download_resource(
        self, resource_key: str, resource_type: str, *, message_id: str
    ) -> bytes | None:
        assert resource_key == "file-key" and resource_type == "file"
        assert message_id == "om_request"
        self.calls += 1
        return self.content


async def test_attachment_ingest_encrypts_parses_and_reuses_managed_blob(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    await repository.upsert_message(
        message(
            attachments=(AttachmentReference("file-key", "file", "notes.txt", "text/plain", 12),)
        )
    )
    cipher = EnvelopeCipher(MasterKey("test", b"i" * 32))
    blobs = EncryptedBlobStore(tmp_path / "blobs", cipher)
    downloader = FakeDownloader(b"private attachment text")
    service = AttachmentService(repository, downloader, blobs, SafeAttachmentParser())
    request = AttachmentLoadRequest("tenant-1", "app-1", "oc_group", "om_request", "file-key")
    try:
        loaded = await service.load(request, now_ms=200)
        again = await service.load(request, now_ms=201)

        assert loaded.evidence.content == "private attachment text"
        assert again.blob_id == loaded.blob_id
        assert downloader.calls == 1
        stored = await repository.get_attachment(
            "tenant-1", "app-1", "oc_group", "om_request", "file-key"
        )
        assert stored is not None
        assert stored.parse_state == "parsed"
        assert stored.parsed_content == "private attachment text"
        ciphertext = await database.call(
            lambda connection: connection.execute(
                "SELECT parsed_content_ciphertext FROM im_attachments"
            ).fetchone()[0]
        )
        assert b"private attachment text" not in ciphertext
        blob_file = next((tmp_path / "blobs").rglob("*.blob"))
        assert b"private attachment text" not in blob_file.read_bytes()
    finally:
        await database.close()


def xlsx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="x"><si><t>Revenue</t></si></sst>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<worksheet xmlns="x"><sheetData><row>'
                '<c r="A1" t="s"><v>0</v></c>'
                '<c r="B1"><f>SUM(B2:B3)</f><v>42</v></c>'
                "</row></sheetData></worksheet>"
            ),
        )
    return output.getvalue()


def stored_attachment(filename: str, *, resource_type: str = "file") -> Any:
    return SimpleNamespace(
        tenant_key="tenant-1",
        app_id="app-1",
        chat_id="oc_group",
        message_id="om_request",
        resource_key="file-key",
        resource_type=resource_type,
        filename=filename,
        media_type=None,
        declared_size=None,
        blob_id=None,
        download_state="referenced",
        parse_state="not_parsed",
    )


def test_safe_attachment_parser_keeps_formulas_inert_and_blocks_active_content() -> None:
    parser = SafeAttachmentParser()
    workbook = parser.parse(stored_attachment("book.xlsx"), xlsx_bytes())
    blocked = parser.parse(stored_attachment("payload.sh"), b"echo unsafe")

    assert workbook.state == "parsed"
    assert workbook.content is not None
    assert "A1=Revenue" in workbook.content
    assert "[formula:SUM(B2:B3)]" in workbook.content
    assert blocked.state == "blocked"
    assert blocked.warning_code == "active_content_blocked"


async def test_attachment_service_rejects_unreferenced_and_oversized_downloads(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    cipher = EnvelopeCipher(MasterKey("test", b"i" * 32))
    service = AttachmentService(
        repository,
        FakeDownloader(b"too large"),
        EncryptedBlobStore(tmp_path / "blobs", cipher),
        SafeAttachmentParser(),
        max_attachment_bytes=4,
    )
    try:
        try:
            await service.load(
                AttachmentLoadRequest("tenant-1", "app-1", "oc_group", "missing", "file-key"),
                now_ms=1,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("unreferenced attachment must be rejected")
        await repository.upsert_message(
            message(attachments=(AttachmentReference("file-key", "file", "notes.txt"),))
        )
        try:
            await service.load(
                AttachmentLoadRequest("tenant-1", "app-1", "oc_group", "om_request", "file-key"),
                now_ms=2,
            )
        except ValueError as exc:
            assert "actual size" in str(exc)
        else:
            raise AssertionError("oversized attachment must be rejected")
    finally:
        await database.close()


class FakeHarnessRunner:
    def __init__(self, outcome: AgentOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def run(self, *args: Any, **kwargs: Any) -> AgentOutcome:
        self.calls += 1
        return self.outcome


def mention_task(task_id: str = "task-1") -> LeasedTask:
    return LeasedTask(
        task_id=task_id,
        event_id="event-1",
        plugin_id="feishu-im",
        command_type="im.handle_mention",
        session_key="tenant-1/app-1/oc_group/omt_thread",
        payload={
            "tenant_key": "tenant-1",
            "app_id": "app-1",
            "chat_id": "oc_group",
            "message_id": "om_request",
            "sender_id": "ou_user",
            "thread_id": "omt_thread",
        },
        attempt_count=1,
        max_attempts=3,
        lease_expires_at_ms=1000,
    )


def response_templates() -> IMResponseTemplates:
    return IMResponseTemplates(
        completed_suffix="Completed. Ask me if anything is unclear.",
        blocked_suffix="I need more information before continuing.",
        failed_suffix="I could not finish this request.",
        cancelled_suffix="This request was cancelled.",
    )


async def test_im_task_handler_renders_verified_terminal_reply_and_reuses_terminal_run(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    trigger = message()
    context = IMContextProvider(FakeLiveIMReader(trigger, ()), repository)
    sessions = InMemorySessionStore()
    harness = FakeHarnessRunner(
        AgentOutcome(
            RunStatus.COMPLETED,
            "The architecture document was created and verified.",
            ("https://example.feishu.cn/docx/docx_1",),
        )
    )
    definition = AgentDefinition(
        "default",
        1,
        "Complete the verified task.",
        "configured-model",
        (),
    )
    handler = IMMentionTaskHandler(
        context=context,
        harness=harness,  # type: ignore[arg-type]
        sessions=sessions,
        definition=definition,
        templates=response_templates(),
        identity_ref="bot-default",
    )
    try:
        result = await handler.execute(mention_task(), now_ms=100)
        assert result.state.value == "succeeded"
        assert result.terminal_message is not None
        assert result.terminal_message.message_kind == "completed"
        assert "created and verified" in str(result.terminal_message.payload["text"])
        assert "https://example.feishu.cn/docx/docx_1" in str(
            result.terminal_message.payload["text"]
        )

        run_id = handler.run_id_for_task("task-recovered")
        await sessions.start_run(
            run_id=run_id,
            task_id="task-recovered",
            session_key="session",
            agent_id="default",
            agent_version=1,
            policy_version=1,
            now_ms=1,
        )
        await sessions.append_event(
            run_id=run_id,
            event_type="run_terminal",
            payload={
                "status": "completed",
                "summary": "Recovered verified outcome.",
                "resource_refs": [],
                "warnings": [],
            },
            now_ms=2,
        )
        await sessions.finish_run(run_id, RunStatus.COMPLETED, now_ms=2)
        recovered = await handler.execute(mention_task("task-recovered"), now_ms=200)
        assert "Recovered verified outcome" in str(recovered.terminal_message.payload["text"])
        assert harness.calls == 1
    finally:
        await database.close()
