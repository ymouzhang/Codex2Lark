from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from codex2lark.core.events import NormalizedEvent, OutboxDraft, TaskCommand, TaskState
from codex2lark.storage.blobs import EncryptedBlobStore
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.runtime_store import RuntimeStore


def cipher() -> EnvelopeCipher:
    return EnvelopeCipher(MasterKey(key_id="test-key", key=b"k" * 32))


def event(*, event_id: str = "event-1") -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        plugin_id="feishu-im",
        event_type="im.message.receive_v1",
        tenant_key="tenant-1",
        app_id="app-1",
        occurred_at_ms=10,
        received_at_ms=20,
        resource_kind="im.message",
        resource_id="message-1",
        trace_id="trace-1",
        source_payload=b'{"secret":"message body"}',
        payload_expires_at_ms=10_000,
    )


def command(*, max_attempts: int = 3) -> TaskCommand:
    return TaskCommand(
        plugin_id="feishu-im",
        command_type="im.handle_mention",
        session_key="tenant-1/app-1/chat-1/root-1",
        payload={"message_id": "message-1", "content": "private request"},
        available_at_ms=100,
        max_attempts=max_attempts,
    )


def acknowledgement() -> OutboxDraft:
    return OutboxDraft(
        publisher_id="feishu-im.reply",
        destination_ref="message-1",
        message_kind="acknowledgement",
        idempotency_key="message-1:ack",
        payload={"text": "I am handling this."},
        available_at_ms=100,
    )


def test_master_key_base64_contract() -> None:
    encoded = base64.b64encode(b"x" * 32).decode()
    key = MasterKey.from_base64(key_id="key-1", encoded_key=encoded)

    assert key.key == b"x" * 32
    with pytest.raises(ValueError, match="32 bytes"):
        MasterKey.from_base64(key_id="key-1", encoded_key=base64.b64encode(b"short").decode())


def test_envelope_cipher_roundtrip_and_aad_binding() -> None:
    value = cipher().encrypt(b"private", associated_data=b"tenant:resource:v1")

    assert b"private" not in value
    assert cipher().decrypt(value, associated_data=b"tenant:resource:v1") == b"private"
    with pytest.raises(InvalidTag):
        cipher().decrypt(value, associated_data=b"other-resource")


def test_envelope_cipher_rejects_tampering() -> None:
    encrypted = cipher().encrypt(b"private", associated_data=b"resource")
    envelope = json.loads(encrypted)
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
    ciphertext[-1] ^= 1
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode()

    with pytest.raises(InvalidTag):
        cipher().decrypt(json.dumps(envelope).encode(), associated_data=b"resource")


def test_blob_store_encrypts_deduplicates_and_enforces_permissions(tmp_path: Path) -> None:
    store = EncryptedBlobStore(tmp_path / "blobs", cipher())

    first = store.put(b"same private bytes")
    second = store.put(b"same private bytes")

    assert first == second
    assert store.get(first) == b"same private bytes"
    blob_path = tmp_path / "blobs" / first[:2] / f"{first}.blob"
    assert b"same private bytes" not in blob_path.read_bytes()
    assert os.stat(blob_path).st_mode & 0o777 == 0o600
    assert store.delete(first)
    assert not store.exists(first)


async def test_database_creates_schema_wal_and_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "data" / "runtime.db"
    database = SQLiteDatabase(path)
    await database.open()
    try:
        journal_mode = await database.call(
            lambda connection: connection.execute("PRAGMA journal_mode").fetchone()[0]
        )
        migration_count = await database.call(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM runtime_migrations"
            ).fetchone()[0]
        )
        assert journal_mode == "wal"
        assert migration_count == 1
        assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        await database.close()


async def test_admission_is_atomic_encrypted_and_deduplicated(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = RuntimeStore(database, cipher())
    try:
        first = await store.admit(event(), command(), acknowledgement=acknowledgement(), now_ms=100)
        duplicate = await store.admit(
            event(), command(), acknowledgement=acknowledgement(), now_ms=101
        )

        assert first.created
        assert duplicate == type(first)(created=False, task_id=first.task_id)
        assert await store.counts() == {
            "runtime_events": 1,
            "runtime_tasks": 1,
            "runtime_outbox": 1,
            "runtime_idempotency": 0,
        }
        ciphertexts = await database.call(
            lambda connection: (
                connection.execute("SELECT payload_ciphertext FROM runtime_events").fetchone()[0],
                connection.execute("SELECT payload_ciphertext FROM runtime_tasks").fetchone()[0],
                connection.execute("SELECT payload_ciphertext FROM runtime_outbox").fetchone()[0],
            )
        )
        assert all(b"private" not in value for value in ciphertexts)
    finally:
        await database.close()


async def test_task_lease_recovery_retry_and_terminal_outbox(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = RuntimeStore(database, cipher())
    try:
        admitted = await store.admit(event(), command(), now_ms=100)
        first = await store.lease_tasks(worker_id="worker-a", now_ms=100, lease_ms=50)
        assert first[0].payload["content"] == "private request"
        assert first[0].attempt_count == 1

        assert await store.lease_tasks(worker_id="worker-b", now_ms=120, lease_ms=50) == []
        recovered = await store.lease_tasks(worker_id="worker-b", now_ms=151, lease_ms=50)
        assert recovered[0].task_id == admitted.task_id
        assert recovered[0].attempt_count == 2

        terminal = OutboxDraft(
            publisher_id="feishu-im.reply",
            destination_ref="message-1",
            message_kind="completed",
            idempotency_key="message-1:terminal",
            payload={"text": "Completed and verified."},
        )
        await store.finish_task(
            admitted.task_id,
            worker_id="worker-b",
            state=TaskState.SUCCEEDED,
            now_ms=160,
            terminal_message=terminal,
        )
        outbox = await store.lease_outbox(worker_id="publisher", now_ms=160, lease_ms=100)
        assert outbox[0].payload == {"text": "Completed and verified."}
        await store.mark_outbox_sent(
            outbox[0].outbox_id,
            worker_id="publisher",
            upstream_ref="om_123",
            now_ms=170,
        )
        state = await database.call(
            lambda connection: connection.execute(
                "SELECT state, upstream_ref FROM runtime_outbox"
            ).fetchone()
        )
        assert tuple(state) == ("sent", "om_123")
    finally:
        await database.close()


async def test_retry_budget_becomes_failed(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = RuntimeStore(database, cipher())
    try:
        admitted = await store.admit(event(), command(max_attempts=1), now_ms=100)
        await store.lease_tasks(worker_id="worker", now_ms=100, lease_ms=10)
        await store.retry_task(
            admitted.task_id,
            worker_id="worker",
            available_at_ms=200,
            now_ms=110,
            error_code="upstream_error",
        )
        state = await database.call(
            lambda connection: connection.execute(
                "SELECT state, last_error_code FROM runtime_tasks"
            ).fetchone()
        )
        assert tuple(state) == ("failed", "upstream_error")
    finally:
        await database.close()


async def test_idempotency_claim_is_owned_and_expires(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = RuntimeStore(database, cipher())
    try:
        first = await store.claim_idempotency(
            key="docs:create:request-1",
            operation_kind="docs.create",
            owner="worker-a",
            expires_at_ms=200,
            now_ms=100,
        )
        duplicate = await store.claim_idempotency(
            key="docs:create:request-1",
            operation_kind="docs.create",
            owner="worker-b",
            expires_at_ms=200,
            now_ms=101,
        )
        assert first.acquired
        assert not duplicate.acquired

        await store.complete_idempotency(
            key="docs:create:request-1",
            owner="worker-a",
            result_ref="docx_123",
            now_ms=110,
        )
        completed = await store.claim_idempotency(
            key="docs:create:request-1",
            operation_kind="docs.create",
            owner="worker-c",
            expires_at_ms=300,
            now_ms=120,
        )
        assert completed.result_ref == "docx_123"

        after_expiry = await store.claim_idempotency(
            key="docs:create:request-1",
            operation_kind="docs.create",
            owner="worker-c",
            expires_at_ms=400,
            now_ms=201,
        )
        assert after_expiry.acquired
    finally:
        await database.close()


async def test_transaction_rolls_back_task_when_outbox_insert_fails(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = RuntimeStore(database, cipher())
    try:
        await store.admit(
            event(event_id="first"), command(), acknowledgement=acknowledgement(), now_ms=1
        )
        conflicting_ack = acknowledgement()
        with pytest.raises(sqlite3.IntegrityError):
            await database.transaction(
                lambda connection: connection.execute(
                    "INSERT INTO runtime_outbox(outbox_id) VALUES ('invalid')"
                )
            )

        assert (await store.counts())["runtime_tasks"] == 1
        assert conflicting_ack.idempotency_key == "message-1:ack"
    finally:
        await database.close()
