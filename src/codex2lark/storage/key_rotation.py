from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .crypto import EnvelopeCipher, MasterKey
from .locking import DataDirectoryLock


@dataclass(frozen=True, slots=True)
class KeyRotationResult:
    ok: bool
    previous_key_id: str
    new_key_id: str
    database_envelopes: int
    blob_envelopes: int


@dataclass(frozen=True, slots=True)
class _EnvelopeSpec:
    table: str
    fields: tuple[str, ...]
    aad: Callable[[sqlite3.Row, str], bytes]


class KeyRotationService:
    MARKER_NAME = "key-rotation.json"

    def __init__(self, data_dir: Path) -> None:
        if not data_dir.is_absolute():
            raise ValueError("data directory must be absolute")
        self.data_dir = data_dir.resolve()
        self.database_path = self.data_dir / "runtime.db"
        self.blob_root = self.data_dir / "blobs"
        self.marker_path = self.data_dir / self.MARKER_NAME

    def rotate(self, current: MasterKey, target: MasterKey) -> KeyRotationResult:
        if current.key_id == target.key_id:
            raise ValueError("new key identifier must differ from the current key")
        if not self.database_path.is_file():
            raise FileNotFoundError(f"runtime database does not exist: {self.database_path}")
        with DataDirectoryLock(self.data_dir):
            self._ensure_marker(current, target)
            cipher = EnvelopeCipher(current)
            database_count = self._rotate_database(cipher, target)
            blob_count = self._rotate_blobs(cipher, target)
            self._verify(target.key_id)
            self.marker_path.unlink()
            return KeyRotationResult(
                True, current.key_id, target.key_id, database_count, blob_count
            )

    def _ensure_marker(self, current: MasterKey, target: MasterKey) -> None:
        expected = {
            "format": "codex2lark-key-rotation-v1",
            "previous_key_id": current.key_id,
            "new_key_id": target.key_id,
        }
        if self.marker_path.exists():
            try:
                existing = json.loads(self.marker_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("key rotation recovery marker is invalid") from exc
            if existing != expected:
                raise RuntimeError("key rotation marker requires the original old/new keys")
            return
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".key-rotation-", dir=self.data_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(expected, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.marker_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def _rotate_database(self, cipher: EnvelopeCipher, target: MasterKey) -> int:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        count = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            for spec in self._specs():
                rows = connection.execute(
                    f"SELECT rowid AS rotation_rowid, * FROM {spec.table}"
                ).fetchall()
                for row in rows:
                    updates: dict[str, bytes] = {}
                    for field in spec.fields:
                        envelope = row[field]
                        if envelope is None:
                            continue
                        rewrapped = cipher.rewrap(
                            envelope,
                            associated_data=spec.aad(row, field),
                            new_master_key=target,
                        )
                        if rewrapped != envelope:
                            updates[field] = rewrapped
                    if updates:
                        assignments = ", ".join(f"{field} = ?" for field in updates)
                        connection.execute(
                            f"UPDATE {spec.table} SET {assignments} WHERE rowid = ?",
                            (*updates.values(), row["rotation_rowid"]),
                        )
                        count += len(updates)
            connection.execute(
                """
                INSERT INTO runtime_admin_audit(
                    operation, target_kind, target_digest, result_counts, created_at_ms
                ) VALUES ('rotate_key', 'storage', ?, ?, ?)
                """,
                (
                    target.key_id,
                    json.dumps({"database_envelopes": count}, separators=(",", ":")),
                    int(time.time() * 1000),
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return count

    def _rotate_blobs(self, cipher: EnvelopeCipher, target: MasterKey) -> int:
        if not self.blob_root.is_dir():
            return 0
        count = 0
        for path in sorted(self.blob_root.glob("*/*.blob")):
            blob_id = path.stem
            envelope = path.read_bytes()
            rewrapped = cipher.rewrap(
                envelope,
                associated_data=f"blob:{blob_id}:v1".encode(),
                new_master_key=target,
            )
            if rewrapped == envelope:
                continue
            descriptor, temporary_name = tempfile.mkstemp(prefix=".rotate-", dir=path.parent)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(rewrapped)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, path)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
            count += 1
        return count

    def _verify(self, target_key_id: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            for spec in self._specs():
                for row in connection.execute(f"SELECT * FROM {spec.table}"):
                    for field in spec.fields:
                        envelope = row[field]
                        if envelope is not None and (
                            EnvelopeCipher.envelope_key_id(envelope) != target_key_id
                        ):
                            raise RuntimeError("database key rotation verification failed")
        for path in self.blob_root.glob("*/*.blob") if self.blob_root.is_dir() else ():
            if EnvelopeCipher.envelope_key_id(path.read_bytes()) != target_key_id:
                raise RuntimeError("blob key rotation verification failed")

    @staticmethod
    def _specs() -> tuple[_EnvelopeSpec, ...]:
        return (
            _EnvelopeSpec(
                "runtime_events",
                ("payload_ciphertext",),
                lambda row, _field: (
                    f"runtime_events:{row['tenant_key']}:{row['app_id']}:"
                    f"{row['event_id']}:v{row['schema_version']}"
                ).encode(),
            ),
            _EnvelopeSpec(
                "runtime_tasks",
                ("payload_ciphertext",),
                lambda row, _field: f"runtime_tasks:{row['task_id']}:v1".encode(),
            ),
            _EnvelopeSpec(
                "runtime_outbox",
                ("payload_ciphertext",),
                lambda row, _field: f"runtime_outbox:{row['outbox_id']}:v1".encode(),
            ),
            _EnvelopeSpec(
                "runtime_run_controls",
                ("payload_ciphertext",),
                lambda row, _field: f"runtime_run_controls:{row['control_id']}:v1".encode(),
            ),
            _EnvelopeSpec(
                "runtime_run_events",
                ("payload_ciphertext",),
                lambda row, _field: (
                    f"runtime_run_events:{row['run_id']}:{row['sequence']}:v1"
                ).encode(),
            ),
            _EnvelopeSpec(
                "runtime_checkpoints",
                ("payload_ciphertext",),
                lambda row, _field: f"runtime_checkpoints:{row['run_id']}:v1".encode(),
            ),
            _EnvelopeSpec(
                "runtime_agent_nodes",
                ("task_brief_ciphertext", "tool_ids_ciphertext", "budget_ciphertext"),
                KeyRotationService._node_aad,
            ),
            _EnvelopeSpec(
                "runtime_mailbox",
                ("payload_ciphertext",),
                lambda row, _field: f"runtime_mailbox:{row['item_id']}:v1".encode(),
            ),
            _EnvelopeSpec(
                "runtime_artifacts",
                ("payload_ciphertext", "source_versions_ciphertext"),
                lambda row, field: (
                    f"runtime_artifacts:{row['artifact_id']}:"
                    f"{'payload' if field == 'payload_ciphertext' else 'sources'}:v1"
                ).encode(),
            ),
            _EnvelopeSpec(
                "runtime_agent_checkpoints",
                ("state_ciphertext",),
                lambda row, _field: (
                    f"runtime_agent_checkpoints:{row['checkpoint_id']}:state:v1"
                ).encode(),
            ),
            _EnvelopeSpec(
                "im_chats",
                ("name_ciphertext",),
                lambda row, _field: (
                    f"im_chats:{row['tenant_key']}:{row['app_id']}:{row['chat_id']}:name:v1"
                ).encode(),
            ),
            _EnvelopeSpec(
                "im_messages",
                ("sender_name_ciphertext", "content_ciphertext", "mentions_ciphertext"),
                KeyRotationService._message_aad,
            ),
            _EnvelopeSpec(
                "im_attachments",
                ("filename_ciphertext", "parsed_content_ciphertext"),
                lambda row, field: (
                    f"im_attachments:{row['tenant_key']}:{row['app_id']}:{row['message_id']}:"
                    f"{row['resource_key']}:"
                    f"{'filename' if field == 'filename_ciphertext' else 'parsed_content'}:v1"
                ).encode(),
            ),
        )

    @staticmethod
    def _node_aad(row: sqlite3.Row, field: str) -> bytes:
        name = {
            "task_brief_ciphertext": "task",
            "tool_ids_ciphertext": "tools",
            "budget_ciphertext": "budget",
        }[field]
        return f"runtime_agent_nodes:{row['node_id']}:{name}:v1".encode()

    @staticmethod
    def _message_aad(row: sqlite3.Row, field: str) -> bytes:
        name = {
            "sender_name_ciphertext": "sender_name",
            "content_ciphertext": "content",
            "mentions_ciphertext": "mentions",
        }[field]
        return (
            f"im_messages:{row['tenant_key']}:{row['app_id']}:{row['message_id']}:{name}:v1"
        ).encode()
