from __future__ import annotations

import asyncio
import io
import logging
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from codex2lark.capabilities.im.admission import IMAdmissionService
from codex2lark.capabilities.im.admission_policy import IMAdmissionPolicy
from codex2lark.capabilities.im.attachments import (
    AttachmentEvidence,
    AttachmentLoadRequest,
    AttachmentService,
    SafeAttachmentParser,
)
from codex2lark.capabilities.im.channel_adapter import (
    ChannelMessageNormalizer,
    OfficialChannelEventSource,
    _DurableDispatcherBridge,
    _install_channel_log_redaction,
    create_official_channel,
)
from codex2lark.capabilities.im.context_provider import (
    IMContextProvider,
    IMContextRequest,
    IMHistoryUnavailableError,
    MessagePage,
)
from codex2lark.capabilities.im.lifecycle import (
    IMLifecycleAdmissionService,
    IMLifecycleTaskHandler,
)
from codex2lark.capabilities.im.live_reader import OfficialIMMessageAPI
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
from codex2lark.core.events import LeasedOutboxMessage, LeasedTask, OutboxDraft
from codex2lark.core.models import Identity
from codex2lark.runtime.context import ContextEvidence
from codex2lark.runtime.outbox import OutboxDispatcher
from codex2lark.runtime.sessions import InMemorySessionStore
from codex2lark.runtime.types import (
    AgentDefinition,
    AgentOutcome,
    MessageRole,
    ModelMessage,
    RunCheckpoint,
    RunStatus,
)
from codex2lark.storage.blobs import EncryptedBlobStore
from codex2lark.storage.capacity import StorageCapacityMonitor, StorageCapacityPolicy
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.runtime_store import RuntimeStore
from codex2lark.storage.session_store import SQLiteSessionStore


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
        assert counts == (1, 0, 0)
    finally:
        await database.close()


async def test_ordinary_human_message_is_observed_without_starting_work(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, service = await setup(tmp_path)
    incoming = message(
        event_id="event-file",
        message_id="om_file",
        message_type="file",
        body_text="",
        mentions=(),
        thread_id=None,
        attachments=(
            AttachmentReference("file-script", "file", "trojan-go_mod1.sh", "text/plain", 12),
        ),
    )
    try:
        decision = await service.admit(incoming)
        stored = await repository.get_message("tenant-1", "app-1", "om_file")
        attachment = await repository.get_attachment(
            "tenant-1", "app-1", "oc_group", "om_file", "file-script"
        )
        counts = await database.call(
            lambda connection: tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("runtime_tasks", "runtime_outbox")
            )
        )

        assert decision.reason is IMAdmissionReason.BOT_NOT_MENTIONED
        assert stored is not None
        assert attachment is not None
        assert attachment.filename == "trojan-go_mod1.sh"
        assert counts == (0, 0)
    finally:
        await database.close()


async def test_admission_policy_rejects_chat_and_actor_before_any_side_effect(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    cipher = EnvelopeCipher(MasterKey("test", b"i" * 32))
    repository = SQLiteIMRepository(database, cipher)
    runtime_store = RuntimeStore(database, cipher)
    service = IMAdmissionService(
        runtime_store,
        repository,
        bot_open_id="ou_bot",
        acknowledgement_text="I will handle this.",
        policy=IMAdmissionPolicy(
            enabled_chat_ids=frozenset({"oc_allowed"}),
            authorized_actor_ids=frozenset({"ou_allowed"}),
        ),
    )
    try:
        denied_chat = await service.admit(message())
        denied_actor = await service.admit(message(chat_id="oc_allowed", sender_id="ou_denied"))
        counts = await database.call(
            lambda connection: tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("im_chats", "im_messages", "runtime_tasks", "runtime_outbox")
            )
        )

        assert denied_chat.reason is IMAdmissionReason.DISABLED_GROUP
        assert denied_actor.reason is IMAdmissionReason.UNAUTHORIZED_ACTOR
        assert counts == (0, 0, 0, 0)
    finally:
        await database.close()


async def test_persisted_disabled_chat_overrides_default_open_policy(tmp_path: Path) -> None:
    database, _repository, _runtime_store, service = await setup(tmp_path)
    try:
        await database.transaction(
            lambda connection: connection.execute(
                """
                INSERT INTO im_chats(
                    tenant_key, app_id, chat_id, chat_mode, enabled,
                    bot_member_state, access_state, last_reconciled_at_ms,
                    retention_policy_id
                ) VALUES ('tenant-1', 'app-1', 'oc_group', 'group', 0,
                          'present', 'visible', 1, 'default')
                """
            )
        )

        denied = await service.admit(message())
        counts = await database.call(
            lambda connection: (
                connection.execute("SELECT COUNT(*) FROM im_messages").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM runtime_tasks").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0],
            )
        )

        assert denied.reason is IMAdmissionReason.DISABLED_GROUP
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


async def test_same_requester_follow_up_becomes_durable_control_without_new_task(
    tmp_path: Path,
) -> None:
    database, _repository, runtime_store, service = await setup(tmp_path)
    try:
        original = await service.admit(message())
        update = message(
            event_id="event-2",
            message_id="om_update",
            body_text="更正：Use the V3 title.",  # noqa: RUF001 - intentional user syntax
            occurred_at_ms=120,
            received_at_ms=130,
        )

        controlled = await service.admit(update)
        duplicate = await service.admit(update)

        assert controlled.task_id == original.task_id
        assert controlled.control_id is not None
        assert controlled.created is True
        assert duplicate.control_id == controlled.control_id
        assert duplicate.created is False
        controls = await runtime_store.pending_controls(str(original.task_id))
        assert [(item.kind.value, item.text) for item in controls] == [
            ("steer", "Use the V3 title.")
        ]
        counts = await database.call(
            lambda connection: (
                connection.execute("SELECT COUNT(*) FROM runtime_tasks").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM runtime_run_controls").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM runtime_outbox").fetchone()[0],
                connection.execute(
                    "SELECT payload_ciphertext FROM runtime_run_controls"
                ).fetchone()[0],
            )
        )
        assert counts[:3] == (1, 1, 2)
        assert b"V3 title" not in counts[3]
    finally:
        await database.close()


async def test_other_participant_cannot_control_active_request(tmp_path: Path) -> None:
    database, _repository, _runtime_store, service = await setup(tmp_path)
    try:
        await service.admit(message())

        decision = await service.admit(
            message(
                event_id="event-2",
                message_id="om_other",
                sender_id="ou_other",
                sender_name="Other",
                body_text="/cancel",
                occurred_at_ms=120,
                received_at_ms=130,
            )
        )

        counts = await database.call(
            lambda connection: (
                connection.execute("SELECT COUNT(*) FROM runtime_tasks").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM runtime_run_controls").fetchone()[0],
            )
        )
        assert decision.control_id is None
        assert counts == (2, 0)
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


def raw_channel_event() -> dict[str, Any]:
    return {
        "header": {
            "event_id": "event-raw",
            "tenant_key": "tenant-1",
            "create_time": "1720000000000",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_user"},
            },
            "message": {
                "chat_id": "oc_group",
                "chat_type": "group",
                "message_id": "om_raw",
                "message_type": "text",
                "create_time": "1720000000000",
                "content": '{"text":"@_user_1 create it"}',
                "mentions": [
                    {
                        "key": "@_user_1",
                        "id": {"open_id": "ou_bot"},
                        "name": "Codex2Lark",
                    }
                ],
            },
        },
    }


def test_raw_channel_normalizer_preserves_durable_admission_fields() -> None:
    normalized = ChannelMessageNormalizer(app_id="app-1", bot_open_id="ou_bot").normalize_raw(
        raw_channel_event(), received_at_ms=1_720_000_000_100
    )

    assert normalized.event_id == "event-raw"
    assert normalized.message_id == "om_raw"
    assert normalized.sender_id == "ou_user"
    assert normalized.body_text == "create it"
    assert normalized.explicitly_mentions("ou_bot")


def test_channel_logger_redacts_websocket_url_and_query_credentials() -> None:
    output = io.StringIO()
    channel_logger = logging.Logger("test-channel")
    channel_logger.addHandler(logging.StreamHandler(output))
    _install_channel_log_redaction(channel_logger)
    _install_channel_log_redaction(channel_logger)

    channel_logger.info(
        "connected to wss://frontier.example/ws?access_key=secret&ticket=secret-ticket"
    )

    logged = output.getvalue()
    assert "wss://" not in logged
    assert "access_key" not in logged
    assert "secret-ticket" not in logged
    assert logged.count("<redacted websocket endpoint>") == 1


async def test_pinned_channel_dispatcher_waits_for_durable_handler() -> None:
    bridge = _DurableDispatcherBridge(lambda value: value)
    bridge.bind_runtime_loop(asyncio.get_running_loop())
    completed: list[str] = []

    async def admit(raw: dict[str, Any]) -> None:
        await asyncio.sleep(0.01)
        completed.append(str(raw["header"]["event_id"]))

    bridge.bind(admit, None, admit, admit, admit)

    errors: list[BaseException] = []

    async def dispatch(callback: Any) -> None:
        def invoke() -> None:
            try:
                callback(raw_channel_event())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        while thread.is_alive():
            await asyncio.sleep(0.001)
        thread.join()

    await dispatch(bridge.dispatch_message)
    await dispatch(bridge.dispatch_message_recalled)
    await dispatch(bridge.dispatch_bot_removed)
    await dispatch(bridge.dispatch_card_action)

    assert errors == []
    assert completed == ["event-raw", "event-raw", "event-raw", "event-raw"]
    bridge.close()


async def test_pinned_channel_bridge_allows_concurrent_group_admission() -> None:
    bridge = _DurableDispatcherBridge(lambda value: value)
    bridge.bind_runtime_loop(asyncio.get_running_loop())
    all_started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum_active = 0

    async def admit(_raw: dict[str, Any]) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 3:
            all_started.set()
        await release.wait()
        active -= 1

    bridge.bind(admit, None, None, None, None)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            bridge.dispatch_message(raw_channel_event())
        except BaseException as exc:
            errors.append(exc)

    callbacks = [threading.Thread(target=invoke) for _ in range(3)]
    for callback in callbacks:
        callback.start()
    await asyncio.wait_for(all_started.wait(), timeout=1)
    release.set()
    while any(callback.is_alive() for callback in callbacks):
        await asyncio.sleep(0.001)
    for callback in callbacks:
        callback.join()

    assert errors == []
    assert maximum_active == 3
    bridge.close()


async def test_pinned_channel_bridge_rejects_runtime_loop_blocking() -> None:
    bridge = _DurableDispatcherBridge(lambda value: value)
    bridge.bind_runtime_loop(asyncio.get_running_loop())

    async def admit(_raw: dict[str, Any]) -> None:
        return None

    bridge.bind(admit, None, None, None, None)

    with pytest.raises(RuntimeError, match="cannot block the Runtime event loop"):
        bridge.dispatch_message(raw_channel_event())
    bridge.close()


def test_pinned_channel_registers_custom_recall_processor() -> None:
    channel = cast(Any, create_official_channel(app_id="cli_test", app_secret="secret"))
    try:
        dispatcher = channel._build_dispatcher()
        assert "p2.im.message.recalled_v1" in dispatcher._processorMap
    finally:
        channel.close_durable_bridge()


async def test_lifecycle_events_are_durable_before_callback_returns(tmp_path: Path) -> None:
    database, _repository, runtime, _service = await setup(tmp_path)
    lifecycle = IMLifecycleAdmissionService(
        runtime, app_id="app-1", received_at_ms=lambda: 1_720_000_000_100
    )
    recall = {
        "header": {
            "event_id": "event-recall",
            "tenant_key": "tenant-1",
            "create_time": "1720000000000",
        },
        "event": {
            "chat_id": "oc_group",
            "message_id": "om_recalled",
            "recall_time": "1720000000050",
        },
    }
    removed = {
        "header": {
            "event_id": "event-removed",
            "tenant_key": "tenant-1",
            "create_time": "1720000000100",
        },
        "event": {"chat_id": "oc_group"},
    }
    try:
        await lifecycle.handle_message_recalled(recall)
        await lifecycle.handle_message_recalled(recall)
        await lifecycle.handle_bot_removed(removed)

        assert (await runtime.counts())["runtime_tasks"] == 2
        command_types = await database.call(
            lambda connection: {
                str(row[0]) for row in connection.execute("SELECT command_type FROM runtime_tasks")
            }
        )
        assert command_types == {
            "im.invalidate_message",
            "im.revoke_chat_access",
        }
    finally:
        await database.close()


async def test_recall_tombstone_cleans_derived_state_and_cannot_be_resurrected(
    tmp_path: Path,
) -> None:
    database, repository, _runtime, admission = await setup(tmp_path)
    cipher = EnvelopeCipher(MasterKey("test", b"i" * 32))
    sessions = SQLiteSessionStore(database, cipher)
    blobs = EncryptedBlobStore(tmp_path / "blobs", cipher)
    incoming = message(updated_at_ms=200)
    try:
        decision = await admission.admit(incoming)
        attachment = await repository.get_attachment(
            "tenant-1", "app-1", "oc_group", "om_request", "file-key"
        )
        assert attachment is not None and decision.task_id is not None
        blob_id = blobs.put(b"private attachment")
        await repository.record_attachment_blob(
            attachment,
            blob_id=blob_id,
            byte_size=18,
            media_type="application/docx",
            now_ms=210,
        )
        await sessions.start_run(
            run_id="run-recall",
            task_id=decision.task_id,
            session_key=incoming.session_key,
            agent_id="agent",
            agent_version=1,
            policy_version=1,
            now_ms=220,
        )
        await sessions.save_checkpoint(
            RunCheckpoint(
                run_id="run-recall",
                agent_id="agent",
                agent_version=1,
                resource_versions={},
                next_turn=2,
                messages=(ModelMessage(MessageRole.USER, "derived"),),
                verified_effects=(),
                blockers=(),
                source_versions={"im.message:om_request": "200"},
                consumed_budget={},
                compactor_version=1,
            ),
            now_ms=230,
        )
        task = LeasedTask(
            task_id="lifecycle-task",
            event_id="event-recall",
            plugin_id="feishu-im",
            command_type="im.invalidate_message",
            session_key="tenant-1/app-1/oc_group/lifecycle/om_request",
            payload={
                "tenant_key": "tenant-1",
                "app_id": "app-1",
                "chat_id": "oc_group",
                "message_id": "om_request",
                "source_version_ms": 300,
            },
            attempt_count=1,
            max_attempts=3,
            lease_expires_at_ms=1_000,
        )

        result = await IMLifecycleTaskHandler(repository, blobs).execute(task, now_ms=310)
        await repository.upsert_message(incoming)

        stored = await repository.get_message("tenant-1", "app-1", "om_request")
        assert result.state.value == "succeeded"
        assert stored is not None and stored.is_recalled
        assert await sessions.load_checkpoint("run-recall") is None
        assert (
            await repository.get_attachment(
                "tenant-1", "app-1", "oc_group", "om_request", "file-key"
            )
            is None
        )
        assert not blobs.exists(blob_id)
    finally:
        await database.close()


async def test_bot_removal_disables_chat_purges_content_and_cancels_pending_work(
    tmp_path: Path,
) -> None:
    database, repository, _runtime, admission = await setup(tmp_path)
    cipher = EnvelopeCipher(MasterKey("test", b"i" * 32))
    blobs = EncryptedBlobStore(tmp_path / "blobs", cipher)
    try:
        await admission.admit(message())
        task = LeasedTask(
            task_id="remove-task",
            event_id="event-removed",
            plugin_id="feishu-im",
            command_type="im.revoke_chat_access",
            session_key="tenant-1/app-1/oc_group/lifecycle/access",
            payload={
                "tenant_key": "tenant-1",
                "app_id": "app-1",
                "chat_id": "oc_group",
            },
            attempt_count=1,
            max_attempts=3,
            lease_expires_at_ms=1_000,
        )

        await IMLifecycleTaskHandler(repository, blobs).execute(task, now_ms=500)
        delayed = await admission.admit(message(event_id="delayed", received_at_ms=600))

        stored = await repository.get_message("tenant-1", "app-1", "om_request")
        state = await database.call(
            lambda connection: (
                tuple(
                    connection.execute(
                        """
                        SELECT enabled, bot_member_state, access_state FROM im_chats
                        WHERE tenant_key = 'tenant-1' AND app_id = 'app-1'
                          AND chat_id = 'oc_group'
                        """
                    ).fetchone()
                ),
                connection.execute(
                    "SELECT state FROM runtime_tasks WHERE command_type = 'im.handle_mention'"
                ).fetchone()[0],
            )
        )
        assert stored is None
        assert delayed.reason is IMAdmissionReason.ACCESS_REVOKED
        assert state == ((0, "removed", "revoked"), "cancelled")
    finally:
        await database.close()


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


async def test_official_channel_source_normalizes_and_admits_before_callback_returns() -> None:
    channel = FakeChannel()
    admission = FakeAdmission()
    source = OfficialChannelEventSource(
        channel,
        admission,
        app_id="app-1",
        received_at_ms=lambda: 500,
    )
    await source.start()
    try:
        await channel.handlers["message"](channel_message())
        assert admission.messages[0].received_at_ms == 500
    finally:
        await source.stop()


async def test_channel_source_gates_admission_and_reports_reconnect_health() -> None:
    channel = FakeChannel()
    admission = FakeAdmission()
    source = OfficialChannelEventSource(
        channel,
        admission,
        app_id="app-1",
        received_at_ms=lambda: 500,
    )
    await source.start()
    try:
        connected = source.health()
        assert connected.ready and connected.state == "connected"

        channel.handlers["reconnecting"]()
        reconnecting = await source.wait_health_change(connected.version)
        assert not reconnecting.ready
        assert reconnecting.state == "reconnecting"
        assert reconnecting.reconnect_attempts == 1

        callback = asyncio.create_task(channel.handlers["message"](channel_message()))
        await asyncio.sleep(0)
        assert not callback.done()
        assert admission.messages == []

        channel.handlers["reconnected"]()
        recovered = await source.wait_health_change(reconnecting.version)
        await callback
        assert recovered.ready and recovered.state == "connected"
        assert len(admission.messages) == 1
    finally:
        await source.stop()


async def test_durable_raw_admission_waits_for_confirmed_reconnect() -> None:
    class DurableFakeChannel(FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.raw_message: Any = None

        def bind_durable_handlers(
            self,
            message: Any,
            bot_added: Any,
            message_recalled: Any,
            bot_removed: Any,
            card_action: Any,
        ) -> None:
            del bot_added, message_recalled, bot_removed, card_action
            self.raw_message = message

    channel = DurableFakeChannel()
    admission = FakeAdmission()
    source = OfficialChannelEventSource(
        channel,
        admission,
        app_id="app-1",
        received_at_ms=lambda: 500,
    )
    await source.start()
    try:
        connected = source.health()
        channel.handlers["reconnecting"]()
        await source.wait_health_change(connected.version)

        callback = asyncio.create_task(channel.raw_message(raw_channel_event()))
        await asyncio.sleep(0)
        assert not callback.done()
        assert admission.messages == []

        channel.handlers["reconnected"]()
        await callback
        assert [item.message_id for item in admission.messages] == ["om_raw"]
    finally:
        await source.stop()


class FakeBotAddedHandler:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def handle_bot_added(self, event: object) -> None:
        self.events.append(event)


async def test_official_channel_source_durably_handles_bot_added_before_return() -> None:
    channel = FakeChannel()
    membership = FakeBotAddedHandler()
    source = OfficialChannelEventSource(
        channel,
        FakeAdmission(),
        app_id="app-1",
        received_at_ms=lambda: 500,
        bot_added_handler=membership,
    )
    event = SimpleNamespace(chat_id="oc_group")
    await source.start()
    try:
        await channel.handlers["botAdded"](event)
        assert membership.events == [event]
    finally:
        await source.stop()


async def test_channel_callback_backpressures_until_admission_finishes() -> None:
    class BlockingAdmission(FakeAdmission):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def admit(self, incoming: IncomingMessage) -> object:
            self.entered.set()
            await self.release.wait()
            return await super().admit(incoming)

    channel = FakeChannel()
    admission = BlockingAdmission()
    source = OfficialChannelEventSource(
        channel,
        admission,
        app_id="app-1",
        received_at_ms=lambda: 500,
    )
    await source.start()
    try:
        callback = asyncio.create_task(channel.handlers["message"](channel_message()))
        await admission.entered.wait()
        assert not callback.done()
        admission.release.set()
        await callback
        assert len(admission.messages) == 1
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
    send_options = channel.sent[0][2]
    request_uuid = send_options.pop("uuid")
    assert isinstance(request_uuid, str)
    assert UUID(request_uuid).version == 5
    assert request_uuid == publisher._request_uuid("stable-key")
    assert send_options == {
        "reply_to": "om_channel",
        "reply_in_thread": True,
        "receive_id_type": "chat_id",
        "reply_target_gone": "fail",
    }
    approval = replace(
        item,
        outbox_id="outbox-2",
        message_kind="approval",
        idempotency_key="approval-key",
        payload={
            "chat_id": "oc_group",
            "message_id": "om_channel",
            "card": {"schema": "2.0", "body": {"elements": []}},
        },
    )
    assert await publisher.publish(approval) == "om_reply"
    assert channel.sent[1][1] == {"card": {"schema": "2.0", "body": {"elements": []}}}
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
        self.recent_requests: list[tuple[int, int]] = []

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
        assert trigger == self.trigger and since_ms >= 0
        self.recent_requests.append((since_ms, limit))
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


async def test_context_provider_continues_only_for_explicit_missing_history_scope(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    trigger = message()

    class MissingHistoryReader(FakeLiveIMReader):
        async def related_messages(self, trigger: IncomingMessage, *, limit: int) -> MessagePage:
            raise IMHistoryUnavailableError("missing im:message.group_msg")

        async def recent_messages(
            self, trigger: IncomingMessage, *, since_ms: int, limit: int
        ) -> MessagePage:
            raise IMHistoryUnavailableError("missing im:message.group_msg")

    try:
        bundle = await IMContextProvider(MissingHistoryReader(trigger, ()), repository).collect(
            IMContextRequest("tenant-1", "app-1", "oc_group", "om_request")
        )

        assert bundle.trigger == trigger
        assert bundle.evidence == ()
        assert bundle.warnings == (
            "im_context_history_unavailable",
            "im_context_incomplete",
        )
        assert await repository.get_message("tenant-1", "app-1", "om_request") is not None
    finally:
        await database.close()


async def test_context_provider_uses_observation_only_to_refetch_named_file_live(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, admission = await setup(tmp_path)
    file_message = message(
        event_id="file-event",
        message_id="om_file",
        message_type="file",
        body_text="",
        mentions=(),
        attachments=(AttachmentReference("file-script", "file", "trojan-go_mod1.sh"),),
        occurred_at_ms=90,
        received_at_ms=91,
        thread_id=None,
    )
    trigger = message(
        body_text="Please analyze trojan-go_mod1.sh",
        attachments=(),
        occurred_at_ms=100,
        thread_id=None,
        root_id=None,
        parent_id=None,
    )

    class HistoryDeniedReader(FakeLiveIMReader):
        async def get_message(self, request: IMContextRequest) -> IncomingMessage:
            if request.message_id == file_message.message_id:
                return file_message
            return await super().get_message(request)

        async def recent_messages(
            self, trigger: IncomingMessage, *, since_ms: int, limit: int
        ) -> MessagePage:
            raise IMHistoryUnavailableError("missing application history scope")

    loader = FakeAttachmentLoader()
    try:
        observed = await admission.admit(file_message)
        bundle = await IMContextProvider(
            HistoryDeniedReader(trigger, ()),
            repository,
            attachments=loader,
            clock_ms=lambda: 500,
        ).collect(IMContextRequest("tenant-1", "app-1", "oc_group", "om_request"))

        assert observed.reason is IMAdmissionReason.BOT_NOT_MENTIONED
        assert len(loader.requests) == 1
        request, _now_ms = loader.requests[0]
        assert isinstance(request, AttachmentLoadRequest)
        assert request.message_id == "om_file"
        assert request.resource_key == "file-script"
        assert "im_context_history_unavailable" in bundle.warnings
    finally:
        await database.close()


def test_live_reader_types_only_missing_group_history_scope() -> None:
    with pytest.raises(IMHistoryUnavailableError):
        OfficialIMMessageAPI._require_success(
            SimpleNamespace(code=230027, request_id="req-1"), "list messages"
        )
    with pytest.raises(RuntimeError, match="code=230027"):
        OfficialIMMessageAPI._require_success(
            SimpleNamespace(code=230027, request_id="req-2"), "get message"
        )


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


async def test_context_provider_resolves_only_exact_named_recent_attachment(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    day_ms = 24 * 60 * 60 * 1000
    trigger = message(
        body_text="Please analyze trojan-go_mod1.sh",
        attachments=(),
        occurred_at_ms=40 * day_ms,
        thread_id=None,
        root_id=None,
        parent_id=None,
    )
    wanted = message(
        event_id="file-event",
        message_id="om_file",
        message_type="file",
        body_text="",
        mentions=(),
        attachments=(AttachmentReference("wanted-key", "file", "trojan-go_mod1.sh"),),
        occurred_at_ms=20 * day_ms,
        thread_id=None,
    )
    unrelated = message(
        event_id="other-event",
        message_id="om_other",
        message_type="file",
        body_text="",
        mentions=(),
        attachments=(AttachmentReference("other-key", "file", "secrets.txt"),),
        occurred_at_ms=19 * day_ms,
        thread_id=None,
    )
    loader = FakeAttachmentLoader()
    source = FakeLiveIMReader(trigger, (unrelated, wanted))
    try:
        bundle = await IMContextProvider(
            source,
            repository,
            attachments=loader,
            clock_ms=lambda: 500,
        ).collect(IMContextRequest("tenant-1", "app-1", "oc_group", "om_request"))

        assert len(loader.requests) == 1
        request, _now_ms = loader.requests[0]
        assert isinstance(request, AttachmentLoadRequest)
        assert request.message_id == "om_file"
        assert request.resource_key == "wanted-key"
        assert source.recent_requests == [(10 * day_ms, 500)]
        assert "im_attachment_ambiguous" not in bundle.warnings
    finally:
        await database.close()


async def test_context_provider_does_not_guess_between_duplicate_filenames(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    trigger = message(
        body_text="Please analyze config.sh",
        attachments=(),
        thread_id=None,
        root_id=None,
        parent_id=None,
    )
    candidates = tuple(
        message(
            event_id=f"file-event-{index}",
            message_id=f"om_file_{index}",
            message_type="file",
            body_text="",
            mentions=(),
            attachments=(AttachmentReference(f"key-{index}", "file", "config.sh"),),
            occurred_at_ms=80 + index,
            thread_id=None,
        )
        for index in range(2)
    )
    loader = FakeAttachmentLoader()
    try:
        bundle = await IMContextProvider(
            FakeLiveIMReader(trigger, candidates), repository, attachments=loader
        ).collect(IMContextRequest("tenant-1", "app-1", "oc_group", "om_request"))

        assert loader.requests == []
        assert "im_attachment_ambiguous" in bundle.warnings
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


async def test_attachment_hard_pressure_keeps_text_request_usable_without_download(
    tmp_path: Path,
) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    await repository.upsert_message(
        message(
            attachments=(AttachmentReference("file-key", "file", "notes.txt", "text/plain", 12),)
        )
    )
    cipher = EnvelopeCipher(MasterKey("test", b"i" * 32))
    downloader = FakeDownloader(b"must not download")
    capacity = StorageCapacityMonitor(
        tmp_path.resolve(),
        StorageCapacityPolicy(
            maximum_managed_bytes=10 * 1024 * 1024,
            minimum_free_bytes=10**18,
        ),
    )
    service = AttachmentService(
        repository,
        downloader,
        EncryptedBlobStore(tmp_path / "blobs", cipher),
        SafeAttachmentParser(),
        capacity=capacity,
    )
    try:
        loaded = await service.load(
            AttachmentLoadRequest("tenant-1", "app-1", "oc_group", "om_request", "file-key"),
            now_ms=200,
        )

        assert downloader.calls == 0
        assert loaded.warning_code == "storage_pressure_hard"
        assert "not downloaded" in loaded.evidence.content
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


def test_safe_attachment_parser_keeps_formulas_and_scripts_inert() -> None:
    parser = SafeAttachmentParser()
    workbook = parser.parse(stored_attachment("book.xlsx"), xlsx_bytes())
    script = parser.parse(stored_attachment("payload.sh"), b"#!/bin/sh\necho inspect-only")
    blocked = parser.parse(stored_attachment("payload.exe"), b"MZ executable")

    assert workbook.state == "parsed"
    assert workbook.content is not None
    assert "A1=Revenue" in workbook.content
    assert "[formula:SUM(B2:B3)]" in workbook.content
    assert script.state == "parsed"
    assert script.content_kind == "source_code"
    assert script.content == "#!/bin/sh\necho inspect-only"
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


async def test_attachment_service_limit_is_inclusive(tmp_path: Path) -> None:
    database, repository, _runtime_store, _service = await setup(tmp_path)
    await repository.upsert_message(
        message(attachments=(AttachmentReference("file-key", "file", "notes.txt", None, 4),))
    )
    service = AttachmentService(
        repository,
        FakeDownloader(b"four"),
        EncryptedBlobStore(tmp_path / "blobs", EnvelopeCipher(MasterKey("test", b"i" * 32))),
        SafeAttachmentParser(),
        max_attachment_bytes=4,
    )
    try:
        loaded = await service.load(
            AttachmentLoadRequest("tenant-1", "app-1", "oc_group", "om_request", "file-key"),
            now_ms=2,
        )
        assert loaded.evidence.content == "four"
    finally:
        await database.close()


class FakeHarnessRunner:
    def __init__(self, outcome: AgentOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def run(self, *args: Any, **kwargs: Any) -> AgentOutcome:
        self.calls += 1
        return self.outcome


class RecordingTaskOutbox:
    def __init__(self) -> None:
        self.items: list[OutboxDraft] = []

    async def enqueue_task_outbox(self, task_id: str, draft: OutboxDraft, *, now_ms: int) -> None:
        del task_id, now_ms
        if draft.idempotency_key not in {item.idempotency_key for item in self.items}:
            self.items.append(draft)


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
        progress_started="I started processing this request.",
        completed_suffix="Completed. Ask me if anything is unclear.",
        blocked_suffix="I need more information before continuing.",
        failed_suffix="I could not finish this request.",
        cancelled_suffix="This request was cancelled.",
    )


def test_im_terminal_renderer_localizes_and_deduplicates_context_warnings() -> None:
    handler = object.__new__(IMMentionTaskHandler)
    handler._templates = response_templates()

    rendered = handler._render(
        AgentOutcome(
            RunStatus.COMPLETED,
            "Answer based on the current message.",
            warnings=(
                "im_context_history_unavailable",
                "im_context_incomplete",
            ),
        )
    )

    assert "im_context_" not in rendered
    assert rendered.count("回答仅基于当前消息") == 1
    assert "230027" in rendered
    assert "im:message.group_msg" in rendered
    assert "机器人仍在群中" in rendered
    assert "应用身份" in rendered
    assert "Completed. Ask me if anything is unclear." in rendered


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
        task_outbox=RecordingTaskOutbox(),
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
        assert "文档链接:" in str(result.terminal_message.payload["text"])

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
