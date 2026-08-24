from __future__ import annotations

import json
import sqlite3
from typing import Any

from codex2lark.runtime.types import (
    AgentOutcome,
    MessageRole,
    ModelMessage,
    RunCheckpoint,
    RunEvent,
    RunStatus,
    ToolCall,
    VerificationRecord,
    VerificationState,
)

from .crypto import EnvelopeCipher
from .database import SQLiteDatabase


class SQLiteSessionStore:
    def __init__(self, database: SQLiteDatabase, cipher: EnvelopeCipher) -> None:
        self._database = database
        self._cipher = cipher

    async def start_run(
        self,
        *,
        run_id: str,
        task_id: str,
        session_key: str,
        agent_id: str,
        agent_version: int,
        policy_version: int,
        now_ms: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO runtime_runs(
                    run_id, task_id, session_key, agent_definition_id,
                    agent_definition_version, policy_version, status,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    session_key,
                    agent_id,
                    agent_version,
                    policy_version,
                    now_ms,
                    now_ms,
                ),
            )

        await self._database.transaction(operation)

    async def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        now_ms: int,
    ) -> RunEvent:
        def operation(connection: sqlite3.Connection) -> RunEvent:
            exists = connection.execute(
                "SELECT 1 FROM runtime_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise LookupError(f"run does not exist: {run_id}")
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM runtime_run_events WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            encrypted = self._encrypt_json(payload, aad=self._event_aad(run_id, sequence))
            connection.execute(
                """
                INSERT INTO runtime_run_events(
                    run_id, sequence, event_type, payload_ciphertext, created_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, sequence, event_type, encrypted, now_ms),
            )
            return RunEvent(run_id, sequence, event_type, payload, now_ms)

        return await self._database.transaction(operation)

    async def save_checkpoint(self, checkpoint: RunCheckpoint, *, now_ms: int) -> None:
        payload = self._checkpoint_dict(checkpoint)
        encrypted = self._encrypt_json(payload, aad=self._checkpoint_aad(checkpoint.run_id))

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO runtime_checkpoints(
                    run_id, payload_ciphertext, next_turn, agent_id, agent_version,
                    compactor_version, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_ciphertext = excluded.payload_ciphertext,
                    next_turn = excluded.next_turn,
                    agent_id = excluded.agent_id,
                    agent_version = excluded.agent_version,
                    compactor_version = excluded.compactor_version,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    checkpoint.run_id,
                    encrypted,
                    checkpoint.next_turn,
                    checkpoint.agent_id,
                    checkpoint.agent_version,
                    checkpoint.compactor_version,
                    now_ms,
                    now_ms,
                ),
            )
            connection.execute(
                "DELETE FROM runtime_checkpoint_sources WHERE run_id = ?",
                (checkpoint.run_id,),
            )
            connection.executemany(
                """
                INSERT INTO runtime_checkpoint_sources(run_id, source_ref, source_version)
                VALUES (?, ?, ?)
                """,
                (
                    (checkpoint.run_id, source_ref, source_version)
                    for source_ref, source_version in sorted(checkpoint.source_versions.items())
                ),
            )

        await self._database.transaction(operation)

    async def discard_checkpoint(self, run_id: str) -> None:
        await self._database.transaction(
            lambda connection: connection.execute(
                "DELETE FROM runtime_checkpoints WHERE run_id = ?", (run_id,)
            )
        )

    async def load_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        row = await self._database.call(
            lambda connection: connection.execute(
                "SELECT payload_ciphertext FROM runtime_checkpoints WHERE run_id = ?", (run_id,)
            ).fetchone()
        )
        if row is None:
            return None
        payload = self._decrypt_json(row["payload_ciphertext"], aad=self._checkpoint_aad(run_id))
        return self._checkpoint_from_dict(payload)

    async def run_status(self, run_id: str) -> RunStatus | None:
        row = await self._database.call(
            lambda connection: connection.execute(
                "SELECT status FROM runtime_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        )
        return None if row is None else RunStatus(row["status"])

    async def load_outcome(self, run_id: str) -> AgentOutcome | None:
        row = await self._database.call(
            lambda connection: connection.execute(
                """
                SELECT sequence, payload_ciphertext FROM runtime_run_events
                WHERE run_id = ? AND event_type = 'run_terminal'
                ORDER BY sequence DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        )
        if row is None:
            return None
        payload = self._decrypt_json(
            row["payload_ciphertext"], aad=self._event_aad(run_id, row["sequence"])
        )
        return AgentOutcome(
            status=RunStatus(str(payload["status"])),
            summary=str(payload["summary"]),
            resource_refs=tuple(str(item) for item in payload["resource_refs"]),
            warnings=tuple(str(item) for item in payload["warnings"]),
        )

    async def finish_run(self, run_id: str, status: RunStatus, *, now_ms: int) -> None:
        if status in (RunStatus.RUNNING, RunStatus.WAITING):
            raise ValueError("finish_run requires a terminal status")

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE runtime_runs SET status = ?, updated_at_ms = ? WHERE run_id = ?",
                (status.value, now_ms, run_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"run does not exist: {run_id}")

        await self._database.transaction(operation)

    async def try_finish_with_outcome(
        self,
        run_id: str,
        outcome: AgentOutcome,
        *,
        applied_control_ids: tuple[str, ...] = (),
        now_ms: int,
    ) -> bool:
        if outcome.status in (RunStatus.RUNNING, RunStatus.WAITING):
            raise ValueError("terminal outcome is required")

        def operation(connection: sqlite3.Connection) -> bool:
            run = connection.execute(
                "SELECT task_id, status FROM runtime_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise LookupError(f"run does not exist: {run_id}")
            if run["status"] != RunStatus.RUNNING.value:
                raise ValueError("run is already terminal")
            if applied_control_ids:
                placeholders = ",".join("?" for _ in applied_control_ids)
                connection.execute(
                    f"""
                    UPDATE runtime_run_controls
                    SET state = 'applied', applied_at_ms = ?
                    WHERE target_task_id = ? AND state = 'pending'
                      AND control_id IN ({placeholders})
                    """,
                    (now_ms, run["task_id"], *applied_control_ids),
                )
            pending = connection.execute(
                """
                SELECT 1 FROM runtime_run_controls
                WHERE target_task_id = ? AND state = 'pending' LIMIT 1
                """,
                (run["task_id"],),
            ).fetchone()
            if pending is not None and outcome.status is RunStatus.COMPLETED:
                return False
            if pending is not None:
                connection.execute(
                    """
                    UPDATE runtime_run_controls
                    SET state = 'superseded', applied_at_ms = ?
                    WHERE target_task_id = ? AND state = 'pending'
                    """,
                    (now_ms, run["task_id"]),
                )
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM runtime_run_events WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            payload = {
                "status": outcome.status.value,
                "summary": outcome.summary,
                "resource_refs": list(outcome.resource_refs),
                "warnings": list(outcome.warnings),
            }
            encrypted = self._encrypt_json(payload, aad=self._event_aad(run_id, sequence))
            connection.execute(
                """
                INSERT INTO runtime_run_events(
                    run_id, sequence, event_type, payload_ciphertext, created_at_ms
                ) VALUES (?, ?, 'run_terminal', ?, ?)
                """,
                (run_id, sequence, encrypted, now_ms),
            )
            connection.execute(
                "UPDATE runtime_runs SET status = ?, updated_at_ms = ? WHERE run_id = ?",
                (outcome.status.value, now_ms, run_id),
            )
            return True

        return await self._database.transaction(operation)

    async def events(self, run_id: str) -> list[RunEvent]:
        rows = await self._database.call(
            lambda connection: connection.execute(
                """
                SELECT sequence, event_type, payload_ciphertext, created_at_ms
                FROM runtime_run_events WHERE run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        )
        return [
            RunEvent(
                run_id=run_id,
                sequence=row["sequence"],
                event_type=row["event_type"],
                payload=self._decrypt_json(
                    row["payload_ciphertext"], aad=self._event_aad(run_id, row["sequence"])
                ),
                created_at_ms=row["created_at_ms"],
            )
            for row in rows
        ]

    def _encrypt_json(self, payload: dict[str, Any], *, aad: bytes) -> bytes:
        return self._cipher.encrypt(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(), associated_data=aad
        )

    def _decrypt_json(self, payload: bytes, *, aad: bytes) -> dict[str, Any]:
        value = json.loads(self._cipher.decrypt(payload, associated_data=aad))
        if not isinstance(value, dict):
            raise ValueError("stored session payload must be an object")
        return value

    @staticmethod
    def _checkpoint_dict(checkpoint: RunCheckpoint) -> dict[str, Any]:
        return {
            "run_id": checkpoint.run_id,
            "agent_id": checkpoint.agent_id,
            "agent_version": checkpoint.agent_version,
            "resource_versions": checkpoint.resource_versions,
            "next_turn": checkpoint.next_turn,
            "messages": [
                {
                    "role": message.role.value,
                    "content": message.content,
                    "name": message.name,
                    "tool_call_id": message.tool_call_id,
                    "trusted": message.trusted,
                    "tool_calls": [
                        {
                            "call_id": call.call_id,
                            "tool_id": call.tool_id,
                            "arguments": call.arguments,
                        }
                        for call in message.tool_calls
                    ],
                }
                for message in checkpoint.messages
            ],
            "verified_effects": [
                {
                    "state": record.state.value,
                    "verifier_id": record.verifier_id,
                    "summary": record.summary,
                    "resource_refs": list(record.resource_refs),
                }
                for record in checkpoint.verified_effects
            ],
            "blockers": list(checkpoint.blockers),
            "source_versions": checkpoint.source_versions,
            "consumed_budget": checkpoint.consumed_budget,
            "compactor_version": checkpoint.compactor_version,
            "applied_control_ids": list(checkpoint.applied_control_ids),
            "unresolved_external_effects": list(checkpoint.unresolved_external_effects),
            "policy_version": checkpoint.policy_version,
            "tool_schema_fingerprint": checkpoint.tool_schema_fingerprint,
        }

    @staticmethod
    def _checkpoint_from_dict(value: dict[str, Any]) -> RunCheckpoint:
        return RunCheckpoint(
            run_id=str(value["run_id"]),
            agent_id=str(value["agent_id"]),
            agent_version=int(value["agent_version"]),
            resource_versions={str(k): str(v) for k, v in value["resource_versions"].items()},
            next_turn=int(value["next_turn"]),
            messages=tuple(
                ModelMessage(
                    role=MessageRole(item["role"]),
                    content=str(item["content"]),
                    name=item.get("name"),
                    tool_call_id=item.get("tool_call_id"),
                    trusted=bool(item.get("trusted", False)),
                    tool_calls=tuple(
                        ToolCall(
                            call_id=str(call["call_id"]),
                            tool_id=str(call["tool_id"]),
                            arguments=dict(call["arguments"]),
                        )
                        for call in item.get("tool_calls", [])
                    ),
                )
                for item in value["messages"]
            ),
            verified_effects=tuple(
                VerificationRecord(
                    state=VerificationState(item["state"]),
                    verifier_id=str(item["verifier_id"]),
                    summary=str(item["summary"]),
                    resource_refs=tuple(str(ref) for ref in item["resource_refs"]),
                )
                for item in value["verified_effects"]
            ),
            blockers=tuple(str(item) for item in value["blockers"]),
            source_versions={str(k): str(v) for k, v in value["source_versions"].items()},
            consumed_budget={str(k): int(v) for k, v in value["consumed_budget"].items()},
            compactor_version=int(value["compactor_version"]),
            applied_control_ids=tuple(str(item) for item in value.get("applied_control_ids", [])),
            unresolved_external_effects=tuple(
                str(item) for item in value.get("unresolved_external_effects", [])
            ),
            policy_version=int(value.get("policy_version", 0)),
            tool_schema_fingerprint=str(value.get("tool_schema_fingerprint", "")),
        )

    @staticmethod
    def _event_aad(run_id: str, sequence: int) -> bytes:
        return f"runtime_run_events:{run_id}:{sequence}:v1".encode()

    @staticmethod
    def _checkpoint_aad(run_id: str) -> bytes:
        return f"runtime_checkpoints:{run_id}:v1".encode()
