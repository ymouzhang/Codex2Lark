from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from codex2lark import cli
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.locking import DataDirectoryLock
from codex2lark.storage.maintenance import StorageMaintenance


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
    assert "content" not in StorageMaintenance.as_json(status)


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


def test_cli_gc_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CODEX2LARK_DATA_DIR", str(tmp_path))

    assert cli.main(["storage", "gc"]) == 1
    assert "requires explicit --yes" in capsys.readouterr().out
