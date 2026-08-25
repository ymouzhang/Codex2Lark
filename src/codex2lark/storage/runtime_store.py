from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from codex2lark.core.events import (
    LeasedOutboxMessage,
    LeasedTask,
    NormalizedEvent,
    OutboxDraft,
    TaskCommand,
    TaskState,
)
from codex2lark.core.ids import new_outbox_id, new_task_id
from codex2lark.core.scheduling import TaskConcurrencyLimits
from codex2lark.runtime.controls import RunControl, RunControlKind

from .crypto import EnvelopeCipher
from .database import SQLiteDatabase


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    created: bool
    task_id: str


@dataclass(frozen=True, slots=True)
class ControlAdmissionResult:
    created: bool
    control_id: str
    task_id: str


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    acquired: bool
    state: str
    result_ref: str | None = None
    recovery_required: bool = False


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
                    tenant_key, app_id, group_id,
                    priority, payload_ciphertext, state, available_at_ms,
                    attempt_count, max_attempts, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?)
                """,
                (
                    task_id,
                    event_pk,
                    command.plugin_id,
                    command.command_type,
                    command.session_key,
                    event.tenant_key,
                    event.app_id,
                    command.group_id,
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

    async def enqueue_task_outbox(self, task_id: str, draft: OutboxDraft, *, now_ms: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            if (
                connection.execute(
                    "SELECT 1 FROM runtime_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                is None
            ):
                raise LookupError("outbox task does not exist")
            self._insert_outbox(connection, draft, task_id=task_id, now_ms=now_ms)

        await self._database.transaction(operation)

    async def request_approval(
        self,
        *,
        approval_id: str,
        task_id: str,
        run_id: str,
        tenant_key: str,
        app_id: str,
        session_key: str,
        actor_id: str,
        tool_id: str,
        argument_digest: str,
        trace_id: str,
        expires_at_ms: int,
        card: OutboxDraft,
        now_ms: int,
    ) -> str:
        def operation(connection: sqlite3.Connection) -> str:
            row = connection.execute(
                "SELECT * FROM runtime_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is not None:
                expected = (task_id, run_id, actor_id, tool_id, argument_digest)
                actual = tuple(
                    row[field]
                    for field in ("task_id", "run_id", "actor_id", "tool_id", "argument_digest")
                )
                if actual != expected:
                    raise RuntimeError("approval identity collision")
                if row["state"] == "pending" and row["expires_at_ms"] <= now_ms:
                    connection.execute(
                        """
                        UPDATE runtime_approvals SET state = 'expired', decided_at_ms = ?
                        WHERE approval_id = ? AND state = 'pending'
                        """,
                        (now_ms, approval_id),
                    )
                    return "expired"
                return str(row["state"])
            connection.execute(
                """
                INSERT INTO runtime_approvals(
                    approval_id, task_id, run_id, tenant_key, app_id, session_key,
                    actor_id, tool_id, argument_digest, state, expires_at_ms,
                    created_at_ms, trace_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    approval_id,
                    task_id,
                    run_id,
                    tenant_key,
                    app_id,
                    session_key,
                    actor_id,
                    tool_id,
                    argument_digest,
                    expires_at_ms,
                    now_ms,
                    trace_id,
                ),
            )
            self._insert_outbox(connection, card, task_id=task_id, now_ms=now_ms)
            return "pending"

        return await self._database.transaction(operation)

    async def decide_approval(
        self,
        event: NormalizedEvent,
        *,
        approval_id: str,
        actor_id: str,
        decision: str,
        acknowledgement: Callable[[str], OutboxDraft],
        now_ms: int,
    ) -> str:
        if decision not in {"approved", "rejected"}:
            raise ValueError("approval decision is invalid")

        def operation(connection: sqlite3.Connection) -> str:
            duplicate = connection.execute(
                """
                SELECT 1 FROM runtime_events
                WHERE tenant_key = ? AND app_id = ? AND event_id = ?
                """,
                (event.tenant_key, event.app_id, event.event_id),
            ).fetchone()
            approval = connection.execute(
                "SELECT * FROM runtime_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if approval is None:
                raise LookupError("approval request does not exist")
            if duplicate is not None:
                return str(approval["state"])
            source_ciphertext = (
                None
                if event.source_payload is None
                else self._cipher.encrypt(
                    event.source_payload, associated_data=self._event_aad(event)
                )
            )
            connection.execute(
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
            state = str(approval["state"])
            authorized = (
                approval["tenant_key"] == event.tenant_key
                and approval["app_id"] == event.app_id
                and approval["actor_id"] == actor_id
            )
            if not authorized:
                result = "unauthorized"
            elif state == "pending" and approval["expires_at_ms"] <= now_ms:
                connection.execute(
                    """
                    UPDATE runtime_approvals SET state = 'expired', decided_at_ms = ?
                    WHERE approval_id = ? AND state = 'pending'
                    """,
                    (now_ms, approval_id),
                )
                result = "expired"
            elif state == "pending":
                connection.execute(
                    """
                    UPDATE runtime_approvals SET state = ?, decided_at_ms = ?
                    WHERE approval_id = ? AND state = 'pending'
                    """,
                    (decision, now_ms, approval_id),
                )
                result = decision
            else:
                result = state
            self._insert_outbox(
                connection,
                acknowledgement(result),
                task_id=str(approval["task_id"]),
                now_ms=now_ms,
            )
            return result

        return await self._database.transaction(operation)

    async def admit_control(
        self,
        event: NormalizedEvent,
        *,
        session_key: str,
        actor_id: str,
        kind: RunControlKind,
        text: str,
        acknowledgement: OutboxDraft,
        now_ms: int,
    ) -> ControlAdmissionResult | None:
        def operation(connection: sqlite3.Connection) -> ControlAdmissionResult | None:
            existing = connection.execute(
                """
                SELECT c.control_id, c.target_task_id
                FROM runtime_events e
                JOIN runtime_run_controls c ON c.event_pk = e.event_pk
                WHERE e.tenant_key = ? AND e.app_id = ? AND e.event_id = ?
                """,
                (event.tenant_key, event.app_id, event.event_id),
            ).fetchone()
            if existing is not None:
                return ControlAdmissionResult(
                    False, existing["control_id"], existing["target_task_id"]
                )
            already_admitted = connection.execute(
                """
                SELECT 1 FROM runtime_events
                WHERE tenant_key = ? AND app_id = ? AND event_id = ?
                """,
                (event.tenant_key, event.app_id, event.event_id),
            ).fetchone()
            if already_admitted is not None:
                return None

            candidates = connection.execute(
                """
                SELECT t.task_id, t.payload_ciphertext
                FROM runtime_tasks t
                LEFT JOIN runtime_runs r ON r.task_id = t.task_id
                WHERE t.session_key = ? AND t.command_type = 'im.handle_mention'
                  AND t.state IN ('pending', 'leased')
                  AND (r.run_id IS NULL OR r.status = 'running')
                ORDER BY CASE t.state WHEN 'leased' THEN 0 ELSE 1 END,
                         t.created_at_ms, t.task_id
                LIMIT 1
                """,
                (session_key,),
            ).fetchall()
            if not candidates:
                return None
            candidate = candidates[0]
            target_task_id = str(candidate["task_id"])
            task_payload = self._decrypt_json(
                candidate["payload_ciphertext"], associated_data=self._task_aad(target_task_id)
            )
            if task_payload.get("sender_id") != actor_id:
                return None

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
                raise RuntimeError("control event insert did not return an identity")
            control_id = str(uuid4())
            payload = self._encrypt_json(
                {
                    "text": text,
                    "actor_id": actor_id,
                    "source_message_id": event.resource_id,
                },
                associated_data=self._control_aad(control_id),
            )
            connection.execute(
                """
                INSERT INTO runtime_run_controls(
                    control_id, event_pk, target_task_id, session_key, kind,
                    payload_ciphertext, state, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    control_id,
                    cursor.lastrowid,
                    target_task_id,
                    session_key,
                    kind.value,
                    payload,
                    now_ms,
                ),
            )
            self._insert_outbox(connection, acknowledgement, task_id=target_task_id, now_ms=now_ms)
            return ControlAdmissionResult(True, control_id, target_task_id)

        return await self._database.transaction(operation)

    async def pending_controls(self, task_id: str) -> tuple[RunControl, ...]:
        rows = await self._database.call(
            lambda connection: connection.execute(
                """
                SELECT control_id, target_task_id, kind, payload_ciphertext, created_at_ms
                FROM runtime_run_controls
                WHERE target_task_id = ? AND state = 'pending'
                ORDER BY created_at_ms, control_id
                """,
                (task_id,),
            ).fetchall()
        )
        result: list[RunControl] = []
        for row in rows:
            control_id = str(row["control_id"])
            payload = self._decrypt_json(
                row["payload_ciphertext"], associated_data=self._control_aad(control_id)
            )
            result.append(
                RunControl(
                    control_id=control_id,
                    target_task_id=str(row["target_task_id"]),
                    kind=RunControlKind(str(row["kind"])),
                    text=str(payload["text"]),
                    actor_id=str(payload["actor_id"]),
                    source_message_id=str(payload["source_message_id"]),
                    created_at_ms=int(row["created_at_ms"]),
                )
            )
        return tuple(result)

    async def acknowledge_controls(
        self, task_id: str, control_ids: tuple[str, ...], *, now_ms: int
    ) -> None:
        if not control_ids:
            return

        def operation(connection: sqlite3.Connection) -> None:
            placeholders = ",".join("?" for _ in control_ids)
            connection.execute(
                f"""
                UPDATE runtime_run_controls
                SET state = 'applied', applied_at_ms = ?
                WHERE target_task_id = ? AND state = 'pending'
                  AND control_id IN ({placeholders})
                """,
                (now_ms, task_id, *control_ids),
            )

        await self._database.transaction(operation)

    async def lease_tasks(
        self,
        *,
        worker_id: str,
        now_ms: int,
        lease_ms: int,
        limit: int = 1,
        limits: TaskConcurrencyLimits | None = None,
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
                SET state = 'pending', last_error_code = 'lease_retry_exhausted',
                    lease_owner = NULL, lease_expires_at_ms = NULL, updated_at_ms = ?
                WHERE state = 'leased' AND lease_expires_at_ms <= ?
                  AND attempt_count >= max_attempts
                """,
                (now_ms, now_ms),
            )
            lease_expires = now_ms + lease_ms
            task_ids = self._select_task_ids(
                connection,
                worker_id=worker_id,
                now_ms=now_ms,
                lease_expires_at_ms=lease_expires,
                limit=limit,
                limits=limits,
            )
            if not task_ids:
                return []
            placeholders = ",".join("?" for _ in task_ids)
            leased = connection.execute(
                f"""
                SELECT t.*, e.event_id, e.trace_id
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
                SET state = 'pending',
                    available_at_ms = ?, lease_owner = NULL, lease_expires_at_ms = NULL,
                    last_error_code = CASE
                        WHEN attempt_count >= max_attempts THEN 'retry_exhausted'
                        ELSE ?
                    END,
                    updated_at_ms = ?
                WHERE task_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (available_at_ms, error_code, now_ms, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("task lease is not owned by worker")

        await self._database.transaction(operation)

    async def defer_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        available_at_ms: int,
        now_ms: int,
        reason: str,
    ) -> None:
        if not reason:
            raise ValueError("task deferral reason is required")

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE runtime_tasks
                SET state = 'pending', available_at_ms = ?,
                    lease_owner = NULL, lease_expires_at_ms = NULL,
                    attempt_count = MAX(attempt_count - 1, 0),
                    last_error_code = ?, updated_at_ms = ?
                WHERE task_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (available_at_ms, reason, now_ms, task_id, worker_id),
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
                SELECT o.outbox_id FROM runtime_outbox o
                WHERE o.state = 'pending' AND o.available_at_ms <= ?
                  AND o.attempt_count < o.max_attempts
                  AND NOT EXISTS (
                      SELECT 1 FROM runtime_outbox prior
                      WHERE prior.task_id = o.task_id
                        AND prior.state != 'sent'
                        AND (
                            prior.created_at_ms < o.created_at_ms
                            OR (
                                prior.created_at_ms = o.created_at_ms
                                AND CASE prior.message_kind
                                    WHEN 'acknowledgement' THEN 0
                                    WHEN 'progress' THEN 1
                                    WHEN 'approval' THEN 2
                                    ELSE 3
                                END < CASE o.message_kind
                                    WHEN 'acknowledgement' THEN 0
                                    WHEN 'progress' THEN 1
                                    WHEN 'approval' THEN 2
                                    ELSE 3
                                END
                            )
                        )
                  )
                ORDER BY o.created_at_ms,
                    CASE o.message_kind
                        WHEN 'acknowledgement' THEN 0
                        WHEN 'progress' THEN 1
                        WHEN 'approval' THEN 2
                        ELSE 3
                    END,
                    o.outbox_id LIMIT ?
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
                ORDER BY created_at_ms,
                    CASE message_kind
                        WHEN 'acknowledgement' THEN 0
                        WHEN 'progress' THEN 1
                        WHEN 'approval' THEN 2
                        ELSE 3
                    END,
                    outbox_id
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

    async def retry_outbox(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        error_code: str,
        available_at_ms: int,
        now_ms: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE runtime_outbox
                SET state = CASE
                        WHEN attempt_count >= max_attempts THEN 'failed'
                        ELSE 'pending'
                    END,
                    available_at_ms = ?, lease_owner = NULL,
                    lease_expires_at_ms = NULL, last_error_code = ?, updated_at_ms = ?
                WHERE outbox_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (available_at_ms, error_code, now_ms, outbox_id, worker_id),
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
            row = connection.execute(
                """
                SELECT state, result_ref, expires_at_ms
                FROM runtime_idempotency WHERE idempotency_key = ?
                """,
                (key,),
            ).fetchone()
            if row is not None:
                if row["state"] == "completed":
                    return IdempotencyClaim(False, "completed", row["result_ref"])
                if row["expires_at_ms"] > now_ms:
                    return IdempotencyClaim(False, row["state"], row["result_ref"])
                connection.execute(
                    """
                    UPDATE runtime_idempotency
                    SET state = 'reconciliation_required', owner = ?,
                        expires_at_ms = ?, updated_at_ms = ?
                    WHERE idempotency_key = ?
                    """,
                    (owner, expires_at_ms, now_ms, key),
                )
                return IdempotencyClaim(True, "reconciliation_required", None, True)
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
                WHERE idempotency_key = ?
                  AND state IN ('claimed', 'reconciliation_required') AND owner = ?
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
            max_attempts=row["max_attempts"],
            lease_expires_at_ms=row["lease_expires_at_ms"],
            recovery_error_code=(
                row["last_error_code"]
                if row["last_error_code"] in {"retry_exhausted", "lease_retry_exhausted"}
                else None
            ),
            tenant_key=row["tenant_key"],
            app_id=row["app_id"],
            group_id=row["group_id"],
            trace_id=row["trace_id"] or task_id,
        )

    def _select_task_ids(
        self,
        connection: sqlite3.Connection,
        *,
        worker_id: str,
        now_ms: int,
        lease_expires_at_ms: int,
        limit: int,
        limits: TaskConcurrencyLimits | None,
    ) -> list[str]:
        if limits is None:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT t.task_id, t.priority, t.created_at_ms,
                           ROW_NUMBER() OVER (
                               PARTITION BY t.session_key
                               ORDER BY t.priority DESC, t.created_at_ms, t.task_id
                           ) AS session_rank
                    FROM runtime_tasks t
                    WHERE t.state = 'pending' AND t.available_at_ms <= ?
                      AND (
                          t.attempt_count < t.max_attempts
                          OR t.last_error_code IN (
                              'retry_exhausted', 'lease_retry_exhausted'
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM runtime_tasks active
                          WHERE active.session_key = t.session_key
                            AND active.state = 'leased'
                            AND active.lease_expires_at_ms > ?
                      )
                )
                SELECT task_id FROM ranked WHERE session_rank = 1
                ORDER BY priority DESC, created_at_ms, task_id LIMIT ?
                """,
                (now_ms, now_ms, limit),
            ).fetchall()
            legacy_task_ids = [str(row["task_id"]) for row in rows]
            for task_id in legacy_task_ids:
                self._lease_task(
                    connection,
                    task_id=task_id,
                    worker_id=worker_id,
                    now_ms=now_ms,
                    lease_expires_at_ms=lease_expires_at_ms,
                )
            return legacy_task_ids

        active = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM runtime_tasks
                WHERE state = 'leased' AND lease_expires_at_ms > ?
                """,
                (now_ms,),
            ).fetchone()[0]
        )
        remaining = min(limit, max(0, limits.global_limit - active))
        task_ids: list[str] = []
        for _ in range(remaining):
            row = connection.execute(
                """
                WITH ranked AS (
                    SELECT t.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY t.session_key
                               ORDER BY t.priority DESC, t.created_at_ms, t.task_id
                           ) AS session_rank,
                           COALESCE(tl.last_served_sequence, 0) AS tenant_served,
                           COALESCE(al.last_served_sequence, 0) AS app_served,
                           COALESCE(gl.last_served_sequence, 0) AS group_served,
                           COALESCE(sl.last_served_sequence, 0) AS session_served
                    FROM runtime_tasks t
                    LEFT JOIN runtime_scheduler_lanes tl
                      ON tl.scope_kind = 'tenant' AND tl.scope_key = t.tenant_key
                    LEFT JOIN runtime_scheduler_lanes al
                      ON al.scope_kind = 'app'
                     AND al.scope_key = t.tenant_key || char(31) || t.app_id
                    LEFT JOIN runtime_scheduler_lanes gl
                      ON gl.scope_kind = 'group'
                     AND gl.scope_key = t.tenant_key || char(31) || t.app_id
                         || char(31) || t.group_id
                    LEFT JOIN runtime_scheduler_lanes sl
                      ON sl.scope_kind = 'session' AND sl.scope_key = t.session_key
                    WHERE t.state = 'pending' AND t.available_at_ms <= ?
                      AND (
                          t.attempt_count < t.max_attempts
                          OR t.last_error_code IN (
                              'retry_exhausted', 'lease_retry_exhausted'
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM runtime_tasks active
                          WHERE active.session_key = t.session_key
                            AND active.state = 'leased'
                            AND active.lease_expires_at_ms > ?
                      )
                )
                SELECT * FROM ranked candidate
                WHERE session_rank = 1
                  AND (
                    SELECT COUNT(*) FROM runtime_tasks active
                    WHERE active.state = 'leased' AND active.lease_expires_at_ms > ?
                      AND active.tenant_key = candidate.tenant_key
                  ) < ?
                  AND (
                    SELECT COUNT(*) FROM runtime_tasks active
                    WHERE active.state = 'leased' AND active.lease_expires_at_ms > ?
                      AND active.tenant_key = candidate.tenant_key
                      AND active.app_id = candidate.app_id
                  ) < ?
                  AND (
                    candidate.group_id IS NULL OR (
                      SELECT COUNT(*) FROM runtime_tasks active
                      WHERE active.state = 'leased' AND active.lease_expires_at_ms > ?
                        AND active.tenant_key = candidate.tenant_key
                        AND active.app_id = candidate.app_id
                        AND active.group_id = candidate.group_id
                    ) < ?
                  )
                ORDER BY tenant_served, app_served, group_served, session_served,
                         priority DESC, created_at_ms, task_id
                LIMIT 1
                """,
                (
                    now_ms,
                    now_ms,
                    now_ms,
                    limits.tenant_limit,
                    now_ms,
                    limits.app_limit,
                    now_ms,
                    limits.group_limit,
                ),
            ).fetchone()
            if row is None:
                break
            task_id = str(row["task_id"])
            self._lease_task(
                connection,
                task_id=task_id,
                worker_id=worker_id,
                now_ms=now_ms,
                lease_expires_at_ms=lease_expires_at_ms,
            )
            self._advance_scheduler_lanes(connection, row, now_ms=now_ms)
            task_ids.append(task_id)
        return task_ids

    @staticmethod
    def _lease_task(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        worker_id: str,
        now_ms: int,
        lease_expires_at_ms: int,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE runtime_tasks
            SET state = 'leased', lease_owner = ?, lease_expires_at_ms = ?,
                attempt_count = CASE
                    WHEN last_error_code IN ('retry_exhausted', 'lease_retry_exhausted')
                    THEN attempt_count ELSE attempt_count + 1
                END,
                updated_at_ms = ?
            WHERE task_id = ? AND state = 'pending'
            """,
            (worker_id, lease_expires_at_ms, now_ms, task_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("selected task could not be leased")

    @staticmethod
    def _advance_scheduler_lanes(
        connection: sqlite3.Connection, row: sqlite3.Row, *, now_ms: int
    ) -> None:
        sequence = (
            int(
                connection.execute(
                    "SELECT next_sequence FROM runtime_scheduler_state WHERE singleton = 1"
                ).fetchone()[0]
            )
            + 1
        )
        connection.execute(
            "UPDATE runtime_scheduler_state SET next_sequence = ? WHERE singleton = 1",
            (sequence,),
        )
        separator = "\x1f"
        tenant_key = str(row["tenant_key"])
        app_id = str(row["app_id"])
        lanes = [
            ("tenant", tenant_key),
            ("app", f"{tenant_key}{separator}{app_id}"),
            ("session", str(row["session_key"])),
        ]
        if row["group_id"] is not None:
            lanes.append(
                (
                    "group",
                    f"{tenant_key}{separator}{app_id}{separator}{row['group_id']}",
                )
            )
        connection.executemany(
            """
            INSERT INTO runtime_scheduler_lanes(
                scope_kind, scope_key, last_served_sequence, updated_at_ms
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(scope_kind, scope_key) DO UPDATE SET
                last_served_sequence = excluded.last_served_sequence,
                updated_at_ms = excluded.updated_at_ms
            """,
            ((kind, key, sequence, now_ms) for kind, key in lanes),
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

    @staticmethod
    def _control_aad(control_id: str) -> bytes:
        return f"runtime_run_controls:{control_id}:v1".encode()
