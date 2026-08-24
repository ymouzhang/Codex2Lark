from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

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
