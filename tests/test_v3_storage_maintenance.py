from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from codex2lark import cli
from codex2lark.capabilities.im.models import IncomingMessage, Mention
from codex2lark.capabilities.im.repository import SQLiteIMRepository
from codex2lark.core.events import NormalizedEvent, TaskCommand
from codex2lark.storage.blobs import EncryptedBlobStore
from codex2lark.storage.capacity import (
    StorageCapacityMonitor,
    StorageCapacityPolicy,
    StoragePressure,
)
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.key_rotation import KeyRotationService
from codex2lark.storage.locking import DataDirectoryLock
from codex2lark.storage.maintenance import StorageMaintenance
from codex2lark.storage.runtime_store import RuntimeStore


async def create_database(data_dir: Path) -> None:
    database = SQLiteDatabase(data_dir / "runtime.db")
    await database.open()
    await database.close()


async def test_status_reports_integrity_without_business_content(tmp_path: Path) -> None:
    data_dir = tmp_path / "state"
    await create_database(data_dir)

    status = StorageMaintenance(data_dir.resolve()).status()

    assert status.ok is True
    assert status.integrity == "ok"
    assert status.schema_version > 0
    assert status.task_states == {}
    assert status.run_states == {}
    assert status.graph_states == {}
    assert status.agent_node_states == {}
    assert status.approval_states == {}
    assert status.task_retry_count == 0
    assert status.oldest_pending_task_age_ms == 0
    assert status.pressure in {"normal", "warning", "hard"}
    assert status.managed_bytes >= status.database_bytes
    assert "content" not in StorageMaintenance.as_json(status)


async def test_status_reports_content_safe_lifecycle_metrics(tmp_path: Path) -> None:
    data_dir = (tmp_path / "state").resolve()
    await create_database(data_dir)
    with sqlite3.connect(data_dir / "runtime.db") as connection:
        connection.execute(
            """
            INSERT INTO runtime_tasks(
                task_id, plugin_id, command_type, session_key, priority,
                payload_ciphertext, state, available_at_ms, attempt_count,
                max_attempts, created_at_ms, updated_at_ms
            ) VALUES ('task', 'im', 'handle', 'session', 0, X'01', 'pending', 100, 2, 5, 100, 100)
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_runs VALUES (
                'run', 'task', 'session', 'root', 1, 1, 'running', 100, 100
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_outbox(
                outbox_id, run_id, task_id, publisher_id, destination_ref,
                message_kind, idempotency_key, payload_ciphertext, state,
                available_at_ms, attempt_count, max_attempts, created_at_ms, updated_at_ms
            ) VALUES (
                'outbox', 'run', 'task', 'im', 'message', 'terminal', 'key', X'02',
                'pending', 200, 3, 8, 200, 200
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_graphs VALUES (
                'graph', 'run', 'node', 'tenant', 'app', 'im.chat', 'chat',
                'root', 1, 'active', 3, 8, 4, 100, 100
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_agent_nodes(
                node_id, graph_id, canonical_path, name, role,
                task_brief_ciphertext, expected_output_type, context_mode,
                tool_ids_ciphertext, budget_ciphertext, depth, status,
                attempt_count, created_at_ms, updated_at_ms
            ) VALUES (
                'node', 'graph', '/root', 'root', 'coordinator', X'01', 'summary',
                'scoped', X'01', X'01', 0, 'running', 1, 100, 100
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runtime_approvals VALUES (
                'approval', 'task', 'run', 'tenant', 'app', 'session', 'actor',
                'tool', 'digest', 'pending', 2000, 100, NULL
            )
            """
        )
        connection.commit()

    status = StorageMaintenance(data_dir).status(now_ms=1_000)
    encoded = StorageMaintenance.as_json(status)

    assert status.task_states == {"pending": 1}
    assert status.outbox_states == {"pending": 1}
    assert status.run_states == {"running": 1}
    assert status.graph_states == {"active": 1}
    assert status.agent_node_states == {"running": 1}
    assert status.approval_states == {"pending": 1}
    assert status.task_retry_count == 2
    assert status.outbox_retry_count == 3
    assert status.oldest_pending_task_age_ms == 900
    assert status.oldest_pending_outbox_age_ms == 800
    assert "payload" not in encoded and "session" not in encoded


def test_capacity_monitor_reports_warning_and_reserved_space_hard_stop(tmp_path: Path) -> None:
    data_dir = (tmp_path / "state").resolve()
    data_dir.mkdir()
    (data_dir / "runtime.db").write_bytes(b"x" * 85)

    warning = StorageCapacityMonitor(
        data_dir,
        StorageCapacityPolicy(
            maximum_managed_bytes=100,
            minimum_free_bytes=0,
            warning_percent=80,
            hard_percent=90,
        ),
    ).snapshot()
    hard = StorageCapacityMonitor(
        data_dir,
        StorageCapacityPolicy(maximum_managed_bytes=1_000_000, minimum_free_bytes=10**18),
    ).snapshot(requested_bytes=1)

    assert warning.pressure is StoragePressure.WARNING
    assert hard.pressure is StoragePressure.HARD
    assert not hard.permits_download


async def test_backup_verify_and_restore_round_trip(tmp_path: Path) -> None:
    data_dir = (tmp_path / "state").resolve()
    await create_database(data_dir)
    archive = (tmp_path / "backup.zip").resolve()

    created = StorageMaintenance(data_dir).backup(archive)
    verified = StorageMaintenance.verify_backup(archive)
    restored_dir = (tmp_path / "restored").resolve()
    restored = StorageMaintenance.restore(archive, restored_dir)

    assert created.ok and verified.ok and restored.ok
    assert created.schema_version == verified.schema_version
    assert StorageMaintenance(restored_dir).status().ok is True
    assert not (restored_dir / "master.key").exists()
    with pytest.raises(FileExistsError):
        StorageMaintenance(data_dir).backup(archive)


async def test_backup_includes_only_referenced_encrypted_blobs(tmp_path: Path) -> None:
    data_dir = (tmp_path / "state").resolve()
    await create_database(data_dir)
    referenced = "a" * 64
    orphan = "b" * 64
    for blob_id in (referenced, orphan):
        path = data_dir / "blobs" / blob_id[:2] / f"{blob_id}.blob"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob_id.encode())
    database = SQLiteDatabase(data_dir / "runtime.db")
    await database.open()
    try:
        await database.transaction(
            lambda connection: connection.executescript(
                f"""
                INSERT INTO im_chats VALUES (
                    'tenant', 'app', 'chat', NULL, 'group', 1, 'joined', 'available',
                    1, 'default', NULL
                );
                INSERT INTO im_messages VALUES (
                    'tenant', 'app', 'message', 'chat', NULL, NULL, NULL, 'user',
                    'sender', NULL, 'file', X'01', X'01', 'hash', 1, 1, 0, 0, 1, 1, NULL
                );
                INSERT INTO im_attachments(
                    tenant_key, app_id, message_id, resource_key, chat_id,
                    resource_type, blob_id, download_state, parse_state
                ) VALUES (
                    'tenant', 'app', 'message', 'resource', 'chat', 'file',
                    '{referenced}', 'downloaded', 'parsed'
                );
                """
            )
        )
    finally:
        await database.close()
    archive = (tmp_path / "backup.zip").resolve()

    StorageMaintenance(data_dir).backup(archive)

    with zipfile.ZipFile(archive) as bundle:
        assert f"blobs/aa/{referenced}.blob" in bundle.namelist()
        assert f"blobs/bb/{orphan}.blob" not in bundle.namelist()


async def test_verify_rejects_undeclared_archive_entry(tmp_path: Path) -> None:
    data_dir = (tmp_path / "state").resolve()
    await create_database(data_dir)
    original = (tmp_path / "backup.zip").resolve()
    StorageMaintenance(data_dir).backup(original)
    tampered = (tmp_path / "tampered.zip").resolve()
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(tampered, "w") as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        target.writestr("../escape", b"unsafe")

    with pytest.raises(RuntimeError, match="entries do not match"):
        StorageMaintenance.verify_backup(tampered)


def test_manifest_contains_no_key_material(tmp_path: Path) -> None:
    archive = (tmp_path / "manual.zip").resolve()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "codex2lark-backup-v1",
                    "schema_version": 999,
                    "files": {},
                }
            ),
        )
    with pytest.raises(RuntimeError, match="newer"):
        StorageMaintenance.verify_backup(archive)


def test_data_directory_has_single_process_owner(tmp_path: Path) -> None:
    first = DataDirectoryLock((tmp_path / "state").resolve())
    second = DataDirectoryLock((tmp_path / "state").resolve())
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="in use"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


async def test_gc_deletes_due_content_but_preserves_shared_blob(tmp_path: Path) -> None:
    data_dir = (tmp_path / "state").resolve()
    await create_database(data_dir)
    blob_id = "c" * 64
    blob = data_dir / "blobs" / "cc" / f"{blob_id}.blob"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"encrypted")
    database = SQLiteDatabase(data_dir / "runtime.db")
    await database.open()
    try:
        await database.transaction(
            lambda connection: connection.executescript(
                f"""
                INSERT INTO im_chats VALUES (
                    'tenant', 'app', 'chat', NULL, 'group', 1, 'joined', 'available',
                    1, 'default', NULL
                );
                INSERT INTO im_messages VALUES (
                    'tenant', 'app', 'due', 'chat', NULL, NULL, NULL, 'user',
                    'sender', NULL, 'file', X'01', X'01', 'hash-1', 1, 1, 0, 0, 1, 1, 10
                );
                INSERT INTO im_messages VALUES (
                    'tenant', 'app', 'retained', 'chat', NULL, NULL, NULL, 'user',
                    'sender', NULL, 'file', X'01', X'01', 'hash-2', 2, 2, 0, 0, 1, 2, NULL
                );
                INSERT INTO im_attachments(
                    tenant_key, app_id, message_id, resource_key, chat_id,
                    resource_type, blob_id, download_state, parse_state, expires_at_ms
                ) VALUES (
                    'tenant', 'app', 'due', 'resource-1', 'chat', 'file',
                    '{blob_id}', 'downloaded', 'parsed', 10
                );
                INSERT INTO im_attachments(
                    tenant_key, app_id, message_id, resource_key, chat_id,
                    resource_type, blob_id, download_state, parse_state, expires_at_ms
                ) VALUES (
                    'tenant', 'app', 'retained', 'resource-2', 'chat', 'file',
                    '{blob_id}', 'downloaded', 'parsed', NULL
                );
                INSERT INTO im_file_blobs VALUES ('{blob_id}', 9, NULL, 1);
                INSERT INTO runtime_idempotency VALUES (
                    'expired', 'test', 'completed', 'owner', NULL, 10, 1, 1
                );
                """
            )
        )
    finally:
        await database.close()

    first = StorageMaintenance(data_dir).garbage_collect(now_ms=10, batch_size=20)

    assert first.messages_deleted == 1
    assert first.attachments_deleted == 1
    assert first.idempotency_deleted == 1
    assert first.blobs_deleted == 0
    assert blob.exists()

    database = SQLiteDatabase(data_dir / "runtime.db")
    await database.open()
    try:
        await database.transaction(
            lambda connection: connection.execute("UPDATE im_attachments SET expires_at_ms = 10")
        )
    finally:
        await database.close()

    second_result = StorageMaintenance(data_dir).garbage_collect(now_ms=10, batch_size=20)

    assert second_result.attachments_deleted == 1
    assert second_result.blobs_deleted == 1
    assert second_result.bytes_reclaimed == len(b"encrypted")
    assert not blob.exists()


async def test_targeted_chat_purge_removes_derived_runtime_and_preserves_shared_blob(
    tmp_path: Path,
) -> None:
    data_dir = (tmp_path / "state").resolve()
    await create_database(data_dir)
    blob_id = "d" * 64
    blob = data_dir / "blobs" / "dd" / f"{blob_id}.blob"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"encrypted-shared")
    database = SQLiteDatabase(data_dir / "runtime.db")
    await database.open()
    try:
        await database.transaction(
            lambda connection: connection.executescript(
                f"""
                INSERT INTO im_chats VALUES
                  ('tenant', 'app', 'chat-1', NULL, 'group', 1, 'present', 'visible',
                   1, 'default', NULL),
                  ('tenant', 'app', 'chat-2', NULL, 'group', 1, 'present', 'visible',
                   1, 'default', NULL);
                INSERT INTO im_messages VALUES
                  ('tenant', 'app', 'message-1', 'chat-1', NULL, NULL, NULL, 'user',
                   'sender', NULL, 'file', X'01', X'01', 'hash-1', 1, 1, 0, 0, 1, 1, NULL),
                  ('tenant', 'app', 'message-2', 'chat-2', NULL, NULL, NULL, 'user',
                   'sender', NULL, 'file', X'01', X'01', 'hash-2', 1, 1, 0, 0, 1, 1, NULL);
                INSERT INTO im_attachments(
                    tenant_key, app_id, message_id, resource_key, chat_id,
                    resource_type, blob_id, download_state, parse_state
                ) VALUES
                  ('tenant', 'app', 'message-1', 'r1', 'chat-1', 'file',
                   '{blob_id}', 'downloaded', 'parsed'),
                  ('tenant', 'app', 'message-2', 'r2', 'chat-2', 'file',
                   '{blob_id}', 'downloaded', 'parsed');
                INSERT INTO im_file_blobs VALUES ('{blob_id}', 16, NULL, 1);
                INSERT INTO runtime_events(
                    event_id, plugin_id, event_type, tenant_key, app_id,
                    occurred_at_ms, received_at_ms, schema_version, resource_kind,
                    resource_id, trace_id, payload_ciphertext, status, created_at_ms
                ) VALUES (
                    'event-1', 'feishu-im', 'im.message.receive_v1', 'tenant', 'app',
                    1, 1, 1, 'im.message', 'message-1', 'trace', X'01', 'admitted', 1
                );
                INSERT INTO runtime_tasks(
                    task_id, event_pk, plugin_id, command_type, session_key, priority,
                    payload_ciphertext, state, available_at_ms, attempt_count,
                    max_attempts, created_at_ms, updated_at_ms
                ) SELECT 'task-1', event_pk, 'feishu-im', 'im.handle_mention',
                    'tenant/app/chat-1/root', 0, X'01', 'pending', 1, 0, 3, 1, 1
                  FROM runtime_events WHERE event_id = 'event-1';
                INSERT INTO runtime_runs VALUES (
                    'run-1', 'task-1', 'tenant/app/chat-1/root', 'agent', 1, 1,
                    'running', 1, 1
                );
                INSERT INTO runtime_checkpoints VALUES (
                    'run-1', X'01', 2, 'agent', 1, 1, 1, 1
                );
                INSERT INTO runtime_checkpoint_sources VALUES (
                    'run-1', 'im.message:message-1', '1'
                );
                INSERT INTO runtime_outbox(
                    outbox_id, run_id, task_id, publisher_id, destination_ref,
                    message_kind, idempotency_key, payload_ciphertext, state,
                    available_at_ms, attempt_count, max_attempts, created_at_ms, updated_at_ms
                ) VALUES (
                    'out-1', 'run-1', 'task-1', 'feishu-im.reply', 'message-1',
                    'completed', 'out-key', X'01', 'pending', 1, 0, 3, 1, 1
                );
                """
            )
        )
    finally:
        await database.close()

    first = StorageMaintenance(data_dir).purge_chat(
        tenant_key="tenant", app_id="app", chat_id="chat-1"
    )

    assert first.messages_deleted == 1
    assert first.attachments_deleted == 1
    assert first.checkpoints_deleted == 1
    assert first.tasks_deleted == 1
    assert first.runs_deleted == 1
    assert first.outbox_deleted == 1
    assert first.blobs_deleted == 0
    assert blob.exists()

    second = StorageMaintenance(data_dir).purge_chat(
        tenant_key="tenant", app_id="app", chat_id="chat-2"
    )

    assert second.blobs_deleted == 1
    assert not blob.exists()
    with pytest.raises(LookupError, match="does not exist"):
        StorageMaintenance(data_dir).purge_chat(tenant_key="tenant", app_id="app", chat_id="chat-2")


async def test_tenant_and_all_purge_remove_exact_business_scopes_and_keep_audit(
    tmp_path: Path,
) -> None:
    data_dir = (tmp_path / "state").resolve()
    await create_database(data_dir)
    blob_id = "e" * 64
    blob = data_dir / "blobs" / "ee" / f"{blob_id}.blob"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"encrypted-shared")
    database = SQLiteDatabase(data_dir / "runtime.db")
    await database.open()
    try:
        await database.transaction(
            lambda connection: connection.executescript(
                f"""
                INSERT INTO im_file_blobs VALUES ('{blob_id}', 16, NULL, 1);
                INSERT INTO im_chats VALUES
                  ('tenant-a', 'app-a', 'chat-a', NULL, 'group', 1, 'present', 'visible',
                   1, 'default', NULL),
                  ('tenant-b', 'app-b', 'chat-b', NULL, 'group', 1, 'present', 'visible',
                   1, 'default', NULL);
                INSERT INTO im_messages VALUES
                  ('tenant-a', 'app-a', 'message-a', 'chat-a', NULL, NULL, NULL, 'user',
                   'sender-a', NULL, 'file', X'01', X'01', 'hash-a', 1, 1, 0, 0, 1, 1, NULL),
                  ('tenant-b', 'app-b', 'message-b', 'chat-b', NULL, NULL, NULL, 'user',
                   'sender-b', NULL, 'file', X'01', X'01', 'hash-b', 1, 1, 0, 0, 1, 1, NULL);
                INSERT INTO im_attachments(
                    tenant_key, app_id, message_id, resource_key, chat_id,
                    resource_type, blob_id, download_state, parse_state
                ) VALUES
                  ('tenant-a', 'app-a', 'message-a', 'resource-a', 'chat-a', 'file',
                   '{blob_id}', 'downloaded', 'parsed'),
                  ('tenant-b', 'app-b', 'message-b', 'resource-b', 'chat-b', 'file',
                   '{blob_id}', 'downloaded', 'parsed');
                INSERT INTO runtime_events(
                    event_id, plugin_id, event_type, tenant_key, app_id,
                    occurred_at_ms, received_at_ms, schema_version, resource_kind,
                    resource_id, trace_id, status, created_at_ms
                ) VALUES
                  ('event-a', 'feishu-im', 'receive', 'tenant-a', 'app-a', 1, 1, 1,
                   'im.message', 'message-a', 'trace-a', 'admitted', 1),
                  ('event-b', 'feishu-im', 'receive', 'tenant-b', 'app-b', 1, 1, 1,
                   'im.message', 'message-b', 'trace-b', 'admitted', 1);
                INSERT INTO runtime_tasks(
                    task_id, event_pk, plugin_id, command_type, session_key, priority,
                    payload_ciphertext, state, available_at_ms, attempt_count,
                    max_attempts, created_at_ms, updated_at_ms
                )
                  SELECT 'task-a', event_pk, 'feishu-im', 'handle',
                    'tenant-a/app-a/chat-a/root', 0, X'01', 'pending', 1, 0, 3, 1, 1
                    FROM runtime_events WHERE event_id = 'event-a'
                  UNION ALL
                  SELECT 'task-b', event_pk, 'feishu-im', 'handle',
                    'tenant-b/app-b/chat-b/root', 0, X'01', 'pending', 1, 0, 3, 1, 1
                    FROM runtime_events WHERE event_id = 'event-b';
                INSERT INTO runtime_runs VALUES
                  ('run-a', 'task-a', 'tenant-a/app-a/chat-a/root', 'agent', 1, 1,
                   'running', 1, 1),
                  ('run-b', 'task-b', 'tenant-b/app-b/chat-b/root', 'agent', 1, 1,
                   'running', 1, 1);
                INSERT INTO runtime_checkpoints VALUES
                  ('run-a', X'01', 2, 'agent', 1, 1, 1, 1),
                  ('run-b', X'01', 2, 'agent', 1, 1, 1, 1);
                INSERT INTO runtime_outbox(
                    outbox_id, run_id, task_id, publisher_id, destination_ref,
                    message_kind, idempotency_key, payload_ciphertext, state,
                    available_at_ms, attempt_count, max_attempts, created_at_ms, updated_at_ms
                ) VALUES
                  ('out-a', 'run-a', 'task-a', 'feishu-im.reply', 'message-a',
                   'completed', 'out-a-key', X'01', 'pending', 1, 0, 3, 1, 1),
                  ('out-b', 'run-b', 'task-b', 'feishu-im.reply', 'message-b',
                   'completed', 'out-b-key', X'01', 'pending', 1, 0, 3, 1, 1);
                INSERT INTO runtime_graphs VALUES
                  ('graph-a', 'run-a', 'root-a', 'tenant-a', 'app-a', 'im.thread',
                   'chat-a', 'agent', 1, 'active', 3, 8, 3, 1, 1),
                  ('graph-b', 'run-b', 'root-b', 'tenant-b', 'app-b', 'im.thread',
                   'chat-b', 'agent', 1, 'active', 3, 8, 3, 1, 1);
                """
            )
        )
    finally:
        await database.close()

    tenant_result = StorageMaintenance(data_dir).purge_tenant(tenant_key="tenant-a")

    assert tenant_result.target_kind == "tenant"
    assert tenant_result.messages_deleted == 1
    assert tenant_result.blobs_deleted == 0
    assert blob.exists()
    with sqlite3.connect(data_dir / "runtime.db") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM im_chats WHERE tenant_key = 'tenant-a'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM im_chats WHERE tenant_key = 'tenant-b'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runtime_graphs WHERE tenant_key = 'tenant-a'"
            ).fetchone()[0]
            == 0
        )
        audit = connection.execute(
            "SELECT target_kind, target_digest, result_counts FROM runtime_admin_audit"
        ).fetchone()
    assert audit[0] == "tenant"
    assert "tenant-a" not in "".join(str(item) for item in audit)

    all_result = StorageMaintenance(data_dir).purge_all()

    assert all_result.target_kind == "all"
    assert all_result.messages_deleted == 1
    assert all_result.blobs_deleted == 1
    assert not blob.exists()
    with sqlite3.connect(data_dir / "runtime.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_migrations").fetchone()[0] == 11
        assert connection.execute("SELECT COUNT(*) FROM runtime_admin_audit").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM runtime_graphs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT target_kind FROM runtime_admin_audit").fetchone()[0] == "all"
        )


def test_cli_gc_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CODEX2LARK_DATA_DIR", str(tmp_path))

    assert cli.main(["storage", "gc"]) == 1
    assert "requires explicit --yes" in capsys.readouterr().out
    assert cli.main(["storage", "purge-tenant", "--tenant-key", "tenant"]) == 1
    assert "requires explicit --yes" in capsys.readouterr().out
    assert cli.main(["storage", "purge-all"]) == 1
    assert "requires explicit --yes" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "storage",
                "purge-message",
                "--tenant-key",
                "tenant",
                "--app-id",
                "app",
                "--message-id",
                "message",
            ]
        )
        == 1
    )
    assert "requires explicit --yes" in capsys.readouterr().out


async def test_key_rotation_rewraps_database_and_blobs_and_is_resumable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = (tmp_path / "state").resolve()
    old = MasterKey("old", b"o" * 32)
    new = MasterKey("new", b"n" * 32)
    database = SQLiteDatabase(data_dir / "runtime.db")
    await database.open()
    old_cipher = EnvelopeCipher(old)
    repository = SQLiteIMRepository(database, old_cipher)
    runtime = RuntimeStore(database, old_cipher)
    incoming = IncomingMessage(
        event_id="message-event",
        tenant_key="tenant",
        app_id="app",
        chat_id="chat",
        chat_type="group",
        message_id="message",
        message_type="text",
        sender_id="user",
        sender_type="user",
        sender_name="Aaron",
        body_text="private body",
        mentions=(Mention("bot"),),
        attachments=(),
        occurred_at_ms=1,
        received_at_ms=1,
    )
    try:
        await repository.upsert_message(incoming)
        await runtime.admit(
            NormalizedEvent(
                event_id="runtime-event",
                plugin_id="feishu-im",
                event_type="im.message.receive_v1",
                tenant_key="tenant",
                app_id="app",
                occurred_at_ms=1,
                received_at_ms=1,
                resource_kind="im.message",
                resource_id="message",
                trace_id="trace",
                source_payload=b"private event",
            ),
            TaskCommand(
                "feishu-im",
                "im.handle_mention",
                "tenant/app/chat/root",
                {"private": "task"},
            ),
            now_ms=1,
        )
    finally:
        await database.close()
    old_blobs = EncryptedBlobStore(data_dir / "blobs", old_cipher)
    blob_id = old_blobs.put(b"private blob")

    interrupted = KeyRotationService(data_dir)
    monkeypatch.setattr(
        interrupted,
        "_rotate_blobs",
        lambda _cipher, _target: (_ for _ in ()).throw(ConnectionError("interrupted")),
    )
    with pytest.raises(ConnectionError, match="interrupted"):
        interrupted.rotate(old, new)
    assert interrupted.marker_path.exists()

    result = KeyRotationService(data_dir).rotate(old, new)

    assert result.ok
    assert result.blob_envelopes == 1
    assert not interrupted.marker_path.exists()
    new_database = SQLiteDatabase(data_dir / "runtime.db")
    await new_database.open()
    try:
        stored = await SQLiteIMRepository(new_database, EnvelopeCipher(new)).get_message(
            "tenant", "app", "message"
        )
        tasks = await RuntimeStore(new_database, EnvelopeCipher(new)).lease_tasks(
            worker_id="worker", now_ms=2, lease_ms=10
        )
        assert stored is not None and stored.body_text == "private body"
        assert tasks[0].payload == {"private": "task"}
    finally:
        await new_database.close()
    rotated_blobs = EncryptedBlobStore(data_dir / "blobs", EnvelopeCipher(new))
    assert rotated_blobs.get(blob_id) == b"private blob"
