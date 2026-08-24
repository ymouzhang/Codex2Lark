from __future__ import annotations

import json
import sqlite3
from typing import Any

from codex2lark.storage.crypto import EnvelopeCipher
from codex2lark.storage.database import SQLiteDatabase

from .models import (
    IMAdmissionReason,
    IncomingMessage,
    Mention,
    StoredAttachment,
    StoredMessage,
)


class SQLiteIMRepository:
    def __init__(self, database: SQLiteDatabase, cipher: EnvelopeCipher) -> None:
        self._database = database
        self._cipher = cipher

    async def chat_admission_denial(
        self, tenant_key: str, app_id: str, chat_id: str
    ) -> IMAdmissionReason | None:
        row = await self._database.call(
            lambda connection: connection.execute(
                """
                SELECT enabled, bot_member_state, access_state FROM im_chats
                WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                """,
                (tenant_key, app_id, chat_id),
            ).fetchone()
        )
        if row is None:
            return None
        if row["access_state"] == "revoked":
            return IMAdmissionReason.ACCESS_REVOKED
        if not bool(row["enabled"]) or row["bot_member_state"] == "removed":
            return IMAdmissionReason.DISABLED_GROUP
        return None

    async def upsert_message(self, message: IncomingMessage) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            self._upsert_chat(connection, message)
            chat = connection.execute(
                """
                SELECT access_state FROM im_chats
                WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                """,
                (message.tenant_key, message.app_id, message.chat_id),
            ).fetchone()
            if chat is not None and chat["access_state"] == "revoked":
                return False
            existing = connection.execute(
                """
                SELECT updated_at_source_ms, is_recalled, is_deleted FROM im_messages
                WHERE tenant_key = ? AND app_id = ? AND message_id = ?
                """,
                (message.tenant_key, message.app_id, message.message_id),
            ).fetchone()
            if existing is not None:
                existing_version = int(existing["updated_at_source_ms"])
                if existing_version > message.source_version_ms or (
                    existing_version == message.source_version_ms
                    and (bool(existing["is_recalled"]) or bool(existing["is_deleted"]))
                    and not (message.is_recalled or message.is_deleted)
                ):
                    return True
                if existing_version != message.source_version_ms:
                    connection.execute(
                        """
                        DELETE FROM runtime_checkpoints WHERE run_id IN (
                            SELECT run_id FROM runtime_checkpoint_sources
                            WHERE source_ref = ? OR source_ref LIKE ?
                        )
                        """,
                        (
                            f"im.message:{message.message_id}",
                            f"im.attachment:{message.message_id}:%",
                        ),
                    )

            content = self._cipher.encrypt(
                message.body_text.encode(),
                associated_data=self._message_aad(message, "content"),
            )
            mentions = self._cipher.encrypt(
                json.dumps(
                    [
                        {"open_id": mention.open_id, "display_name": mention.display_name}
                        for mention in message.mentions
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode(),
                associated_data=self._message_aad(message, "mentions"),
            )
            sender_name = self._encrypt_optional(
                message.sender_name,
                aad=self._message_aad(message, "sender_name"),
            )
            connection.execute(
                """
                INSERT INTO im_messages(
                    tenant_key, app_id, message_id, chat_id, thread_id, root_id,
                    parent_id, sender_type, sender_id, sender_name_ciphertext,
                    message_type, content_ciphertext, mentions_ciphertext, content_hash,
                    created_at_source_ms, updated_at_source_ms, is_recalled,
                    is_deleted, schema_version, last_reconciled_at_ms, expires_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(tenant_key, app_id, message_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    thread_id = excluded.thread_id,
                    root_id = excluded.root_id,
                    parent_id = excluded.parent_id,
                    sender_type = excluded.sender_type,
                    sender_id = excluded.sender_id,
                    sender_name_ciphertext = excluded.sender_name_ciphertext,
                    message_type = excluded.message_type,
                    content_ciphertext = excluded.content_ciphertext,
                    mentions_ciphertext = excluded.mentions_ciphertext,
                    content_hash = excluded.content_hash,
                    updated_at_source_ms = excluded.updated_at_source_ms,
                    is_recalled = excluded.is_recalled,
                    is_deleted = excluded.is_deleted,
                    last_reconciled_at_ms = excluded.last_reconciled_at_ms,
                    expires_at_ms = excluded.expires_at_ms
                """,
                (
                    message.tenant_key,
                    message.app_id,
                    message.message_id,
                    message.chat_id,
                    message.thread_id,
                    message.root_id,
                    message.parent_id,
                    message.sender_type,
                    message.sender_id,
                    sender_name,
                    message.message_type,
                    content,
                    mentions,
                    self._cipher.opaque_digest(message.body_text.encode()),
                    message.occurred_at_ms,
                    message.source_version_ms,
                    int(message.is_recalled),
                    int(message.is_deleted),
                    message.received_at_ms,
                    message.received_at_ms + 90 * 24 * 60 * 60 * 1000,
                ),
            )
            connection.execute(
                """
                DELETE FROM im_attachments
                WHERE tenant_key = ? AND app_id = ? AND message_id = ?
                """,
                (message.tenant_key, message.app_id, message.message_id),
            )
            for attachment in message.attachments:
                filename = self._encrypt_optional(
                    attachment.filename,
                    aad=self._attachment_aad(message, attachment.resource_key, "filename"),
                )
                connection.execute(
                    """
                    INSERT INTO im_attachments(
                        tenant_key, app_id, message_id, resource_key, chat_id,
                        resource_type, filename_ciphertext, media_type, declared_size,
                        download_state, parse_state, expires_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'referenced', 'not_parsed', ?)
                    """,
                    (
                        message.tenant_key,
                        message.app_id,
                        message.message_id,
                        attachment.resource_key,
                        message.chat_id,
                        attachment.resource_type,
                        filename,
                        attachment.media_type,
                        attachment.declared_size,
                        message.received_at_ms + 30 * 24 * 60 * 60 * 1000,
                    ),
                )

            return True

        return await self._database.transaction(operation)

    async def get_message(
        self, tenant_key: str, app_id: str, message_id: str
    ) -> StoredMessage | None:
        row = await self._database.call(
            lambda connection: connection.execute(
                """
                SELECT * FROM im_messages
                WHERE tenant_key = ? AND app_id = ? AND message_id = ?
                """,
                (tenant_key, app_id, message_id),
            ).fetchone()
        )
        return None if row is None else self._message(row)

    async def invalidate_message(
        self,
        *,
        tenant_key: str,
        app_id: str,
        chat_id: str,
        message_id: str,
        source_version_ms: int,
        now_ms: int,
    ) -> tuple[str, ...]:
        """Write a monotonic recall tombstone and remove its derived state."""

        def operation(connection: sqlite3.Connection) -> tuple[str, ...]:
            existing = connection.execute(
                """
                SELECT updated_at_source_ms FROM im_messages
                WHERE tenant_key = ? AND app_id = ? AND message_id = ?
                """,
                (tenant_key, app_id, message_id),
            ).fetchone()
            if existing is not None and existing["updated_at_source_ms"] > source_version_ms:
                return ()
            candidates = self._attachment_blob_ids(
                connection, tenant_key=tenant_key, app_id=app_id, message_id=message_id
            )
            connection.execute(
                """
                INSERT INTO im_chats(
                    tenant_key, app_id, chat_id, chat_mode, enabled,
                    bot_member_state, access_state, last_reconciled_at_ms,
                    retention_policy_id
                ) VALUES (?, ?, ?, 'unknown', 1, 'present', 'visible', ?, 'default')
                ON CONFLICT(tenant_key, app_id, chat_id) DO NOTHING
                """,
                (tenant_key, app_id, chat_id, now_ms),
            )
            if existing is None:
                content = self._cipher.encrypt(
                    b"",
                    associated_data=self._message_identity_aad(
                        tenant_key, app_id, message_id, "content"
                    ),
                )
                mentions = self._cipher.encrypt(
                    b"[]",
                    associated_data=self._message_identity_aad(
                        tenant_key, app_id, message_id, "mentions"
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO im_messages(
                        tenant_key, app_id, message_id, chat_id, sender_type,
                        sender_id, message_type, content_ciphertext,
                        mentions_ciphertext, content_hash, created_at_source_ms,
                        updated_at_source_ms, is_recalled, is_deleted,
                        schema_version, last_reconciled_at_ms, expires_at_ms
                    ) VALUES (?, ?, ?, ?, 'unknown', 'unknown', 'unknown', ?, ?, ?,
                              ?, ?, 1, 0, 1, ?, ?)
                    """,
                    (
                        tenant_key,
                        app_id,
                        message_id,
                        chat_id,
                        content,
                        mentions,
                        self._cipher.opaque_digest(b""),
                        source_version_ms,
                        source_version_ms,
                        now_ms,
                        now_ms + 90 * 24 * 60 * 60 * 1000,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE im_messages
                    SET is_recalled = 1, updated_at_source_ms = ?,
                        last_reconciled_at_ms = ?
                    WHERE tenant_key = ? AND app_id = ? AND message_id = ?
                    """,
                    (source_version_ms, now_ms, tenant_key, app_id, message_id),
                )
            connection.execute(
                """
                DELETE FROM runtime_checkpoints WHERE run_id IN (
                    SELECT run_id FROM runtime_checkpoint_sources
                    WHERE source_ref = ? OR source_ref LIKE ?
                )
                """,
                (f"im.message:{message_id}", f"im.attachment:{message_id}:%"),
            )
            connection.execute(
                """
                DELETE FROM im_attachments
                WHERE tenant_key = ? AND app_id = ? AND message_id = ?
                """,
                (tenant_key, app_id, message_id),
            )
            return self._drop_unreferenced_blob_metadata(connection, candidates)

        return await self._database.transaction(operation)

    async def revoke_chat_access(
        self,
        *,
        tenant_key: str,
        app_id: str,
        chat_id: str,
        now_ms: int,
    ) -> tuple[str, ...]:
        """Disable a chat and purge all locally derived business content."""

        def operation(connection: sqlite3.Connection) -> tuple[str, ...]:
            candidates = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT blob_id FROM im_attachments
                    WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                      AND blob_id IS NOT NULL
                    """,
                    (tenant_key, app_id, chat_id),
                )
            )
            message_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT message_id FROM im_messages
                    WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                    """,
                    (tenant_key, app_id, chat_id),
                )
            )
            if message_ids:
                source_refs = tuple(f"im.message:{item}" for item in message_ids)
                placeholders = ",".join("?" for _ in source_refs)
                connection.execute(
                    f"""
                    DELETE FROM runtime_checkpoints WHERE run_id IN (
                        SELECT run_id FROM runtime_checkpoint_sources
                        WHERE source_ref IN ({placeholders})
                    )
                    """,
                    source_refs,
                )
            connection.execute(
                """
                DELETE FROM im_messages
                WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                """,
                (tenant_key, app_id, chat_id),
            )
            connection.execute(
                """
                INSERT INTO im_chats(
                    tenant_key, app_id, chat_id, chat_mode, enabled,
                    bot_member_state, access_state, last_reconciled_at_ms,
                    retention_policy_id, purge_after_ms
                ) VALUES (?, ?, ?, 'unknown', 0, 'removed', 'revoked', ?, 'default', ?)
                ON CONFLICT(tenant_key, app_id, chat_id) DO UPDATE SET
                    enabled = 0, bot_member_state = 'removed',
                    access_state = 'revoked',
                    last_reconciled_at_ms = excluded.last_reconciled_at_ms,
                    purge_after_ms = excluded.purge_after_ms
                """,
                (tenant_key, app_id, chat_id, now_ms, now_ms),
            )
            connection.execute(
                """
                UPDATE runtime_tasks SET state = 'cancelled', updated_at_ms = ?
                WHERE session_key LIKE ? AND state = 'pending'
                  AND command_type != 'im.revoke_chat_access'
                """,
                (now_ms, f"{tenant_key}/{app_id}/{chat_id}/%"),
            )
            return self._drop_unreferenced_blob_metadata(connection, candidates)

        return await self._database.transaction(operation)

    async def restore_chat_access(
        self, *, tenant_key: str, app_id: str, chat_id: str, now_ms: int
    ) -> None:
        await self._database.transaction(
            lambda connection: connection.execute(
                """
                INSERT INTO im_chats(
                    tenant_key, app_id, chat_id, chat_mode, enabled,
                    bot_member_state, access_state, last_reconciled_at_ms,
                    retention_policy_id, purge_after_ms
                ) VALUES (?, ?, ?, 'unknown', 1, 'present', 'visible', ?, 'default', NULL)
                ON CONFLICT(tenant_key, app_id, chat_id) DO UPDATE SET
                    enabled = 1, bot_member_state = 'present', access_state = 'visible',
                    last_reconciled_at_ms = excluded.last_reconciled_at_ms,
                    purge_after_ms = NULL
                """,
                (tenant_key, app_id, chat_id, now_ms),
            )
        )

    async def recent_messages(
        self,
        tenant_key: str,
        app_id: str,
        chat_id: str,
        *,
        before_ms: int,
        limit: int = 30,
    ) -> list[StoredMessage]:
        if limit < 1 or limit > 100:
            raise ValueError("message context limit must be between 1 and 100")
        rows = await self._database.call(
            lambda connection: connection.execute(
                """
                SELECT * FROM im_messages
                WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                  AND created_at_source_ms <= ? AND is_recalled = 0 AND is_deleted = 0
                ORDER BY created_at_source_ms DESC, message_id DESC LIMIT ?
                """,
                (tenant_key, app_id, chat_id, before_ms, limit),
            ).fetchall()
        )
        return [self._message(row) for row in reversed(rows)]

    async def get_attachment(
        self,
        tenant_key: str,
        app_id: str,
        chat_id: str,
        message_id: str,
        resource_key: str,
    ) -> StoredAttachment | None:
        row = await self._database.call(
            lambda connection: connection.execute(
                """
                SELECT * FROM im_attachments
                WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                  AND message_id = ? AND resource_key = ?
                """,
                (tenant_key, app_id, chat_id, message_id, resource_key),
            ).fetchone()
        )
        return None if row is None else self._attachment(row)

    async def record_attachment_blob(
        self,
        attachment: StoredAttachment,
        *,
        blob_id: str,
        byte_size: int,
        media_type: str | None,
        now_ms: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT OR IGNORE INTO im_file_blobs(blob_id, byte_size, media_type, created_at_ms)
                VALUES (?, ?, ?, ?)
                """,
                (blob_id, byte_size, media_type, now_ms),
            )
            cursor = connection.execute(
                """
                UPDATE im_attachments
                SET blob_id = ?, media_type = COALESCE(?, media_type),
                    download_state = 'downloaded'
                WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                  AND message_id = ? AND resource_key = ?
                """,
                (
                    blob_id,
                    media_type,
                    attachment.tenant_key,
                    attachment.app_id,
                    attachment.chat_id,
                    attachment.message_id,
                    attachment.resource_key,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError("attachment reference disappeared during ingest")

        await self._database.transaction(operation)

    async def record_attachment_parse(
        self,
        attachment: StoredAttachment,
        *,
        parser_id: str,
        parser_version: str,
        parsing_policy_version: str,
        content: str | None,
        state: str,
        warning_code: str | None,
    ) -> None:
        if state not in {"parsed", "metadata_only", "blocked", "failed"}:
            raise ValueError("invalid attachment parse state")
        encrypted = self._encrypt_optional(
            content,
            aad=self._attachment_identity_aad(
                attachment.tenant_key,
                attachment.app_id,
                attachment.message_id,
                attachment.resource_key,
                "parsed_content",
            ),
        )
        content_hash = None if content is None else self._cipher.opaque_digest(content.encode())

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE im_attachments
                SET parse_state = ?, parser_id = ?, parser_version = ?,
                    parsing_policy_version = ?,
                    parsed_content_ciphertext = ?, parsed_content_hash = ?, warning_code = ?
                WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                  AND message_id = ? AND resource_key = ?
                """,
                (
                    state,
                    parser_id,
                    parser_version,
                    parsing_policy_version,
                    encrypted,
                    content_hash,
                    warning_code,
                    attachment.tenant_key,
                    attachment.app_id,
                    attachment.chat_id,
                    attachment.message_id,
                    attachment.resource_key,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError("attachment reference disappeared during parse")

        await self._database.transaction(operation)

    def _upsert_chat(self, connection: sqlite3.Connection, message: IncomingMessage) -> None:
        name = self._encrypt_optional(
            message.chat_name,
            aad=self._chat_aad(message.tenant_key, message.app_id, message.chat_id),
        )
        connection.execute(
            """
            INSERT INTO im_chats(
                tenant_key, app_id, chat_id, name_ciphertext, chat_mode, enabled,
                bot_member_state, access_state, last_reconciled_at_ms,
                retention_policy_id
            ) VALUES (?, ?, ?, ?, ?, 1, 'present', 'visible', ?, 'default')
            ON CONFLICT(tenant_key, app_id, chat_id) DO UPDATE SET
                name_ciphertext = COALESCE(excluded.name_ciphertext, im_chats.name_ciphertext),
                chat_mode = excluded.chat_mode,
                enabled = im_chats.enabled,
                bot_member_state = CASE WHEN im_chats.access_state = 'revoked'
                                        THEN im_chats.bot_member_state ELSE 'present' END,
                access_state = CASE WHEN im_chats.access_state = 'revoked'
                                    THEN im_chats.access_state ELSE 'visible' END,
                last_reconciled_at_ms = excluded.last_reconciled_at_ms
            """,
            (
                message.tenant_key,
                message.app_id,
                message.chat_id,
                name,
                message.chat_type,
                message.received_at_ms,
            ),
        )

    @staticmethod
    def _attachment_blob_ids(
        connection: sqlite3.Connection,
        *,
        tenant_key: str,
        app_id: str,
        message_id: str,
    ) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT blob_id FROM im_attachments
                WHERE tenant_key = ? AND app_id = ? AND message_id = ?
                  AND blob_id IS NOT NULL
                """,
                (tenant_key, app_id, message_id),
            )
        )

    @staticmethod
    def _drop_unreferenced_blob_metadata(
        connection: sqlite3.Connection, candidates: tuple[str, ...]
    ) -> tuple[str, ...]:
        unreferenced: list[str] = []
        for blob_id in candidates:
            if (
                connection.execute(
                    "SELECT 1 FROM im_attachments WHERE blob_id = ? LIMIT 1", (blob_id,)
                ).fetchone()
                is None
            ):
                connection.execute("DELETE FROM im_file_blobs WHERE blob_id = ?", (blob_id,))
                unreferenced.append(blob_id)
        return tuple(unreferenced)

    def _message(self, row: sqlite3.Row) -> StoredMessage:
        identity = (row["tenant_key"], row["app_id"], row["message_id"])
        content = self._cipher.decrypt(
            row["content_ciphertext"],
            associated_data=self._message_identity_aad(*identity, "content"),
        ).decode()
        raw_mentions: Any = json.loads(
            self._cipher.decrypt(
                row["mentions_ciphertext"],
                associated_data=self._message_identity_aad(*identity, "mentions"),
            )
        )
        if not isinstance(raw_mentions, list):
            raise ValueError("stored mentions must be a list")
        mentions = tuple(
            Mention(str(item["open_id"]), item.get("display_name"))
            for item in raw_mentions
            if isinstance(item, dict) and item.get("open_id")
        )
        sender_name = self._decrypt_optional(
            row["sender_name_ciphertext"],
            aad=self._message_identity_aad(*identity, "sender_name"),
        )
        return StoredMessage(
            tenant_key=row["tenant_key"],
            app_id=row["app_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            message_type=row["message_type"],
            sender_id=row["sender_id"],
            sender_type=row["sender_type"],
            sender_name=sender_name,
            body_text=content,
            mentions=mentions,
            occurred_at_ms=row["created_at_source_ms"],
            source_version_ms=row["updated_at_source_ms"],
            thread_id=row["thread_id"],
            root_id=row["root_id"],
            parent_id=row["parent_id"],
            is_recalled=bool(row["is_recalled"]),
            is_deleted=bool(row["is_deleted"]),
        )

    def _attachment(self, row: sqlite3.Row) -> StoredAttachment:
        identity = (
            row["tenant_key"],
            row["app_id"],
            row["message_id"],
            row["resource_key"],
        )
        return StoredAttachment(
            tenant_key=row["tenant_key"],
            app_id=row["app_id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            resource_key=row["resource_key"],
            resource_type=row["resource_type"],
            filename=self._decrypt_optional(
                row["filename_ciphertext"],
                aad=self._attachment_identity_aad(*identity, "filename"),
            ),
            media_type=row["media_type"],
            declared_size=row["declared_size"],
            blob_id=row["blob_id"],
            download_state=row["download_state"],
            parse_state=row["parse_state"],
            parsed_content=self._decrypt_optional(
                row["parsed_content_ciphertext"],
                aad=self._attachment_identity_aad(*identity, "parsed_content"),
            ),
            warning_code=row["warning_code"],
        )

    def _encrypt_optional(self, value: str | None, *, aad: bytes) -> bytes | None:
        return None if value is None else self._cipher.encrypt(value.encode(), associated_data=aad)

    def _decrypt_optional(self, value: bytes | None, *, aad: bytes) -> str | None:
        return None if value is None else self._cipher.decrypt(value, associated_data=aad).decode()

    def _message_aad(self, message: IncomingMessage, field: str) -> bytes:
        return self._message_identity_aad(
            message.tenant_key, message.app_id, message.message_id, field
        )

    @staticmethod
    def _message_identity_aad(tenant_key: str, app_id: str, message_id: str, field: str) -> bytes:
        return f"im_messages:{tenant_key}:{app_id}:{message_id}:{field}:v1".encode()

    @staticmethod
    def _chat_aad(tenant_key: str, app_id: str, chat_id: str) -> bytes:
        return f"im_chats:{tenant_key}:{app_id}:{chat_id}:name:v1".encode()

    @staticmethod
    def _attachment_aad(message: IncomingMessage, resource_key: str, field: str) -> bytes:
        return SQLiteIMRepository._attachment_identity_aad(
            message.tenant_key,
            message.app_id,
            message.message_id,
            resource_key,
            field,
        )

    @staticmethod
    def _attachment_identity_aad(
        tenant_key: str,
        app_id: str,
        message_id: str,
        resource_key: str,
        field: str,
    ) -> bytes:
        return (
            f"im_attachments:{tenant_key}:{app_id}:{message_id}:{resource_key}:{field}:v1"
        ).encode()
