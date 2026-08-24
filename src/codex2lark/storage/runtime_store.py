from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from codex2lark.core.events import (
    LeasedOutboxMessage,
    LeasedTask,
    NormalizedEvent,
    OutboxDraft,
    TaskCommand,
    TaskState,
)
from codex2lark.core.ids import new_outbox_id, new_task_id

from .crypto import EnvelopeCipher
from .database import SQLiteDatabase


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    created: bool
    task_id: str


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    acquired: bool
    state: str
    result_ref: str | None = None


class RuntimeStore:
    def __init__(self, database: SQLiteDatabase, cipher: EnvelopeCipher) -> None:
        self._database = database
        self._cipher = cipher

    async def admit(
        self,
        event: NormalizedEvent,
        command: TaskCommand,
        *,
        acknowledgement: OutboxDraft | None = None,
        now_ms: int,
    ) -> AdmissionResult:
        def operation(connection: sqlite3.Connection) -> AdmissionResult:
            existing = connection.execute(
                """
                SELECT t.task_id
                FROM runtime_events e
                JOIN runtime_tasks t ON t.event_pk = e.event_pk
                WHERE e.tenant_key = ? AND e.app_id = ? AND e.event_id = ?
                """,
                (event.tenant_key, event.app_id, event.event_id),
            ).fetchone()
            if existing is not None:
                return AdmissionResult(created=False, task_id=existing["task_id"])

            source_ciphertext = None
            if event.source_payload is not None:
                source_ciphertext = self._cipher.encrypt(
                    event.source_payload,
                    associated_data=self._event_aad(event),
                )
            cursor = connection.execute(
                """
                INSERT INTO runtime_events(
                    event_id, plugin_id, event_type, tenant_key, app_id,
                    occurred_at_ms, received_at_ms, schema_version, resource_kind,
                    resource_id, correlation_id, trace_id, payload_ciphertext,
                    payload_expires_at_ms, status, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted', ?)
                """,
                (
                    event.event_id,
                    event.plugin_id,
                    event.event_type,
                    event.tenant_key,
                    event.app_id,
                    event.occurred_at_ms,
                    event.received_at_ms,
                    event.schema_version,
                    event.resource_kind,
                    event.resource_id,
                    event.correlation_id,
                    event.trace_id,
                    source_ciphertext,
                    event.payload_expires_at_ms,
                    now_ms,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("event insert did not return an identity")
            event_pk = cursor.lastrowid
            task_id = str(new_task_id())
            task_ciphertext = self._encrypt_json(
                command.payload, associated_data=self._task_aad(task_id)
            )
            connection.execute(
                """
                INSERT INTO runtime_tasks(
                    task_id, event_pk, plugin_id, command_type, session_key,
                    priority, payload_ciphertext, state, available_at_ms,
                    attempt_count, max_attempts, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?)
                """,
                (
                    task_id,
                    event_pk,
                    command.plugin_id,
                    command.command_type,
                    command.session_key,
                    command.priority,
                    task_ciphertext,
                    command.available_at_ms,
                    command.max_attempts,
                    now_ms,
                    now_ms,
                ),
            )
            if acknowledgement is not None:
                self._insert_outbox(connection, acknowledgement, task_id=task_id, now_ms=now_ms)
            return AdmissionResult(created=True, task_id=task_id)

        return await self._database.transaction(operation)

    async def lease_tasks(
        self,
        *,
        worker_id: str,
        now_ms: int,
        lease_ms: int,
        limit: int = 1,
    ) -> list[LeasedTask]:
        if not worker_id or lease_ms <= 0 or limit <= 0:
            raise ValueError("worker_id, lease_ms, and limit must be positive")

        def operation(connection: sqlite3.Connection) -> list[LeasedTask]:
            connection.execute(
                """
                UPDATE runtime_tasks
                SET state = 'pending', lease_owner = NULL, lease_expires_at_ms = NULL,
                    updated_at_ms = ?
                WHERE state = 'leased' AND lease_expires_at_ms <= ?
                  AND attempt_count < max_attempts
                """,
                (now_ms, now_ms),
            )
            connection.execute(
                """
                UPDATE runtime_tasks
                SET state = 'failed', last_error_code = 'retry_exhausted',
                    lease_owner = NULL, lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE state = 'leased' AND lease_expires_at_ms <= ?
                  AND attempt_count >= max_attempts
                """,
                (now_ms, now_ms),
            )
            rows = connection.execute(
                """
                SELECT task_id FROM runtime_tasks
                WHERE state = 'pending' AND available_at_ms <= ?
                  AND attempt_count < max_attempts
                ORDER BY priority DESC, created_at_ms, task_id
                LIMIT ?
                """,
                (now_ms, limit),
            ).fetchall()
            task_ids = [row["task_id"] for row in rows]
            lease_expires = now_ms + lease_ms
            for task_id in task_ids:
                connection.execute(
                    """
                    UPDATE runtime_tasks
                    SET state = 'leased', lease_owner = ?, lease_expires_at_ms = ?,
                        attempt_count = attempt_count + 1, updated_at_ms = ?
                    WHERE task_id = ? AND state = 'pending'
                    """,
                    (worker_id, lease_expires, now_ms, task_id),
                )
            if not task_ids:
                return []
            placeholders = ",".join("?" for _ in task_ids)
            leased = connection.execute(
                f"""
                SELECT t.*, e.event_id
                FROM runtime_tasks t
                LEFT JOIN runtime_events e ON e.event_pk = t.event_pk
                WHERE t.task_id IN ({placeholders}) AND t.lease_owner = ?
                ORDER BY t.priority DESC, t.created_at_ms, t.task_id
                """,
                (*task_ids, worker_id),
            ).fetchall()
            return [self._leased_task(row) for row in leased]

        return await self._database.transaction(operation)

    async def finish_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        state: TaskState,
        now_ms: int,
        error_code: str | None = None,
        terminal_message: OutboxDraft | None = None,
    ) -> None:
        if state not in {
            TaskState.SUCCEEDED,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }:
            raise ValueError("finish_task requires a terminal task state")

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE runtime_tasks
                SET state = ?, lease_owner = NULL, lease_expires_at_ms = NULL,
                    last_error_code = ?, updated_at_ms = ?
                WHERE task_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (state.value, error_code, now_ms, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("task lease is not owned by worker")
            if terminal_message is not None:
                self._insert_outbox(
                    connection,
                    terminal_message,
                    task_id=task_id,
                    now_ms=now_ms,
                )

        await self._database.transaction(operation)

    async def retry_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        available_at_ms: int,
        now_ms: int,
        error_code: str,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE runtime_tasks
                SET state = CASE
                        WHEN attempt_count >= max_attempts THEN 'failed'
                        ELSE 'pending'
                    END,
                    available_at_ms = ?, lease_owner = NULL, lease_expires_at_ms = NULL,
                    last_error_code = ?, updated_at_ms = ?
                WHERE task_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (available_at_ms, error_code, now_ms, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("task lease is not owned by worker")

        await self._database.transaction(operation)

    async def lease_outbox(
        self,
        *,
        worker_id: str,
        now_ms: int,
        lease_ms: int,
        limit: int = 10,
    ) -> list[LeasedOutboxMessage]:
        if not worker_id or lease_ms <= 0 or limit <= 0:
            raise ValueError("worker_id, lease_ms, and limit must be positive")

        def operation(connection: sqlite3.Connection) -> list[LeasedOutboxMessage]:
            connection.execute(
                """
                UPDATE runtime_outbox
                SET state = 'pending', lease_owner = NULL, lease_expires_at_ms = NULL,
                    updated_at_ms = ?
                WHERE state = 'leased' AND lease_expires_at_ms <= ?
                  AND attempt_count < max_attempts
                """,
                (now_ms, now_ms),
            )
            connection.execute(
                """
                UPDATE runtime_outbox
                SET state = 'failed', last_error_code = 'retry_exhausted',
                    lease_owner = NULL, lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE state = 'leased' AND lease_expires_at_ms <= ?
                  AND attempt_count >= max_attempts
                """,
                (now_ms, now_ms),
            )
            rows = connection.execute(
                """
                SELECT outbox_id FROM runtime_outbox
                WHERE state = 'pending' AND available_at_ms <= ?
                  AND attempt_count < max_attempts
                ORDER BY created_at_ms, outbox_id LIMIT ?
                """,
                (now_ms, limit),
            ).fetchall()
            ids = [row["outbox_id"] for row in rows]
            expiry = now_ms + lease_ms
            for outbox_id in ids:
                connection.execute(
                    """
                    UPDATE runtime_outbox
                    SET state = 'leased', lease_owner = ?, lease_expires_at_ms = ?,
                        attempt_count = attempt_count + 1, updated_at_ms = ?
                    WHERE outbox_id = ? AND state = 'pending'
                    """,
                    (worker_id, expiry, now_ms, outbox_id),
                )
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            leased = connection.execute(
                f"""
                SELECT * FROM runtime_outbox
                WHERE outbox_id IN ({placeholders}) AND lease_owner = ?
                ORDER BY created_at_ms, outbox_id
                """,
                (*ids, worker_id),
            ).fetchall()
            return [self._leased_outbox(row) for row in leased]

        return await self._database.transaction(operation)

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        upstream_ref: str,
        now_ms: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE runtime_outbox
                SET state = 'sent', upstream_ref = ?, lease_owner = NULL,
                    lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE outbox_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (upstream_ref, now_ms, outbox_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("outbox lease is not owned by worker")

        await self._database.transaction(operation)

    async def claim_idempotency(
        self,
        *,
        key: str,
        operation_kind: str,
        owner: str,
        expires_at_ms: int,
        now_ms: int,
    ) -> IdempotencyClaim:
        def operation(connection: sqlite3.Connection) -> IdempotencyClaim:
            connection.execute(
                "DELETE FROM runtime_idempotency WHERE expires_at_ms <= ?", (now_ms,)
            )
            row = connection.execute(
                "SELECT state, result_ref FROM runtime_idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                return IdempotencyClaim(False, row["state"], row["result_ref"])
            connection.execute(
                """
                INSERT INTO runtime_idempotency(
                    idempotency_key, operation_kind, state, owner, expires_at_ms,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, 'claimed', ?, ?, ?, ?)
                """,
                (key, operation_kind, owner, expires_at_ms, now_ms, now_ms),
            )
            return IdempotencyClaim(True, "claimed")

        return await self._database.transaction(operation)

    async def complete_idempotency(
        self,
        *,
        key: str,
        owner: str,
        result_ref: str,
        now_ms: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE runtime_idempotency
                SET state = 'completed', result_ref = ?, updated_at_ms = ?
                WHERE idempotency_key = ? AND state = 'claimed' AND owner = ?
                """,
                (result_ref, now_ms, key, owner),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("idempotency claim is not owned by caller")

        await self._database.transaction(operation)

    async def counts(self) -> dict[str, int]:
        def operation(connection: sqlite3.Connection) -> dict[str, int]:
            tables = ("runtime_events", "runtime_tasks", "runtime_outbox", "runtime_idempotency")
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }

        return await self._database.call(operation)

    def _insert_outbox(
        self,
        connection: sqlite3.Connection,
        draft: OutboxDraft,
        *,
        task_id: str,
        now_ms: int,
    ) -> str:
        outbox_id = str(new_outbox_id())
        payload = self._encrypt_json(draft.payload, associated_data=self._outbox_aad(outbox_id))
        connection.execute(
            """
            INSERT OR IGNORE INTO runtime_outbox(
                outbox_id, task_id, publisher_id, destination_ref, message_kind,
                idempotency_key, payload_ciphertext, state, available_at_ms,
                attempt_count, max_attempts, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, 8, ?, ?)
            """,
            (
                outbox_id,
                task_id,
                draft.publisher_id,
                draft.destination_ref,
                draft.message_kind,
                draft.idempotency_key,
                payload,
                draft.available_at_ms,
                now_ms,
                now_ms,
            ),
        )
        return outbox_id

    def _leased_task(self, row: sqlite3.Row) -> LeasedTask:
        task_id = row["task_id"]
        return LeasedTask(
            task_id=task_id,
            event_id=row["event_id"],
            plugin_id=row["plugin_id"],
            command_type=row["command_type"],
            session_key=row["session_key"],
            payload=self._decrypt_json(
                row["payload_ciphertext"], associated_data=self._task_aad(task_id)
            ),
            attempt_count=row["attempt_count"],
            lease_expires_at_ms=row["lease_expires_at_ms"],
        )

    def _leased_outbox(self, row: sqlite3.Row) -> LeasedOutboxMessage:
        outbox_id = row["outbox_id"]
        return LeasedOutboxMessage(
            outbox_id=outbox_id,
            run_id=row["run_id"],
            publisher_id=row["publisher_id"],
            destination_ref=row["destination_ref"],
            message_kind=row["message_kind"],
            idempotency_key=row["idempotency_key"],
            payload=self._decrypt_json(
                row["payload_ciphertext"], associated_data=self._outbox_aad(outbox_id)
            ),
            attempt_count=row["attempt_count"],
            lease_expires_at_ms=row["lease_expires_at_ms"],
        )

    def _encrypt_json(self, value: dict[str, Any], *, associated_data: bytes) -> bytes:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return self._cipher.encrypt(payload.encode("utf-8"), associated_data=associated_data)

    def _decrypt_json(self, value: bytes, *, associated_data: bytes) -> dict[str, Any]:
        decoded = json.loads(self._cipher.decrypt(value, associated_data=associated_data))
        if not isinstance(decoded, dict):
            raise ValueError("stored payload must be an object")
        return decoded

    @staticmethod
    def _event_aad(event: NormalizedEvent) -> bytes:
        return (
            f"runtime_events:{event.tenant_key}:{event.app_id}:{event.event_id}:"
            f"v{event.schema_version}"
        ).encode()

    @staticmethod
    def _task_aad(task_id: str) -> bytes:
        return f"runtime_tasks:{task_id}:v1".encode()

    @staticmethod
    def _outbox_aad(outbox_id: str) -> bytes:
        return f"runtime_outbox:{outbox_id}:v1".encode()
