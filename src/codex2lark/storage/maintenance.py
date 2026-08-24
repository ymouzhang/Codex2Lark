from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .capacity import StorageCapacityMonitor, StorageCapacityPolicy
from .key_rotation import KeyRotationResult
from .locking import DataDirectoryLock
from .migrations import SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class StorageStatus:
    ok: bool
    integrity: str
    schema_version: int
    database_bytes: int
    blob_count: int
    blob_bytes: int
    task_states: dict[str, int]
    outbox_states: dict[str, int]
    run_states: dict[str, int]
    graph_states: dict[str, int]
    agent_node_states: dict[str, int]
    approval_states: dict[str, int]
    task_retry_count: int
    outbox_retry_count: int
    oldest_pending_task_age_ms: int
    oldest_pending_outbox_age_ms: int
    pressure: str
    managed_bytes: int
    maximum_managed_bytes: int
    filesystem_free_bytes: int


@dataclass(frozen=True, slots=True)
class BackupResult:
    ok: bool
    archive: str
    schema_version: int
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class GarbageCollectionResult:
    ok: bool
    event_payloads_cleared: int
    messages_deleted: int
    attachments_deleted: int
    artifacts_deleted: int
    idempotency_deleted: int
    checkpoints_deleted: int
    blobs_deleted: int
    bytes_reclaimed: int


@dataclass(frozen=True, slots=True)
class PurgeResult:
    ok: bool
    target_kind: str
    messages_deleted: int
    attachments_deleted: int
    checkpoints_deleted: int
    tasks_deleted: int
    runs_deleted: int
    outbox_deleted: int
    blobs_deleted: int
    bytes_reclaimed: int


class StorageMaintenance:
    def __init__(
        self, data_dir: Path, capacity_policy: StorageCapacityPolicy | None = None
    ) -> None:
        if not data_dir.is_absolute():
            raise ValueError("data directory must be absolute")
        self.data_dir = data_dir.resolve()
        self.database_path = self.data_dir / "runtime.db"
        self.blob_root = self.data_dir / "blobs"
        self._capacity = StorageCapacityMonitor(
            self.data_dir, capacity_policy or StorageCapacityPolicy.from_environment()
        )

    def status(self, *, now_ms: int | None = None) -> StorageStatus:
        capacity = self._capacity.snapshot()
        observed_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if not self.database_path.is_file():
            return StorageStatus(
                False,
                "database_missing",
                0,
                0,
                0,
                0,
                {},
                {},
                {},
                {},
                {},
                {},
                0,
                0,
                0,
                0,
                capacity.pressure.value,
                capacity.managed_bytes,
                capacity.maximum_managed_bytes,
                capacity.filesystem_free_bytes,
            )
        with self._readonly(self.database_path) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            schema_version = self._schema_version(connection)
            task_states = self._state_counts(connection, "runtime_tasks")
            outbox_states = self._state_counts(connection, "runtime_outbox")
            run_states = self._state_counts(connection, "runtime_runs")
            graph_states = self._state_counts(connection, "runtime_graphs")
            agent_node_states = self._state_counts(connection, "runtime_agent_nodes")
            approval_states = self._state_counts(connection, "runtime_approvals")
            task_retry_count = self._sum_attempts(connection, "runtime_tasks")
            outbox_retry_count = self._sum_attempts(connection, "runtime_outbox")
            oldest_pending_task_age_ms = self._oldest_pending_age(
                connection, "runtime_tasks", observed_at_ms
            )
            oldest_pending_outbox_age_ms = self._oldest_pending_age(
                connection, "runtime_outbox", observed_at_ms
            )
            blob_ids = self._referenced_blob_ids(connection)
        blob_paths = [self._blob_path(self.blob_root, blob_id) for blob_id in blob_ids]
        present = [path for path in blob_paths if path.is_file()]
        blobs_complete = len(present) == len(blob_paths)
        return StorageStatus(
            ok=integrity == "ok" and blobs_complete and schema_version <= SCHEMA_VERSION,
            integrity=(integrity if blobs_complete else "referenced_blob_missing"),
            schema_version=schema_version,
            database_bytes=self.database_path.stat().st_size,
            blob_count=len(present),
            blob_bytes=sum(path.stat().st_size for path in present),
            task_states=task_states,
            outbox_states=outbox_states,
            run_states=run_states,
            graph_states=graph_states,
            agent_node_states=agent_node_states,
            approval_states=approval_states,
            task_retry_count=task_retry_count,
            outbox_retry_count=outbox_retry_count,
            oldest_pending_task_age_ms=oldest_pending_task_age_ms,
            oldest_pending_outbox_age_ms=oldest_pending_outbox_age_ms,
            pressure=capacity.pressure.value,
            managed_bytes=capacity.managed_bytes,
            maximum_managed_bytes=capacity.maximum_managed_bytes,
            filesystem_free_bytes=capacity.filesystem_free_bytes,
        )

    def backup(self, archive: Path) -> BackupResult:
        archive = self._absolute(archive, "backup archive")
        if archive.exists():
            raise FileExistsError(f"backup archive already exists: {archive}")
        if not self.database_path.is_file():
            raise FileNotFoundError(f"runtime database does not exist: {self.database_path}")
        archive.parent.mkdir(parents=True, exist_ok=True)
        with (
            DataDirectoryLock(self.data_dir),
            tempfile.TemporaryDirectory(prefix=".codex2lark-backup-", dir=archive.parent) as raw,
        ):
            staging = Path(raw)
            snapshot = staging / "runtime.db"
            with sqlite3.connect(self.database_path) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            with self._readonly(snapshot) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"backup database integrity failed: {integrity}")
                schema_version = self._schema_version(connection)
                blob_ids = self._referenced_blob_ids(connection)

            files: dict[str, Path] = {"runtime.db": snapshot}
            for blob_id in blob_ids:
                source_blob = self._blob_path(self.blob_root, blob_id)
                if not source_blob.is_file():
                    raise RuntimeError(f"referenced encrypted blob is missing: {blob_id}")
                files[f"blobs/{blob_id[:2]}/{blob_id}.blob"] = source_blob
            manifest = {
                "format": "codex2lark-backup-v1",
                "created_at_ms": int(time.time() * 1000),
                "schema_version": schema_version,
                "files": {
                    name: {"bytes": path.stat().st_size, "sha256": self._digest(path)}
                    for name, path in sorted(files.items())
                },
            }
            total_bytes = sum(path.stat().st_size for path in files.values())
            temporary = staging / "backup.zip"
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as bundle:
                for name, path in files.items():
                    bundle.write(path, name)
                bundle.writestr(
                    "manifest.json",
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                )
            os.chmod(temporary, 0o600)
            os.replace(temporary, archive)
        return BackupResult(
            True,
            str(archive),
            schema_version,
            len(files),
            total_bytes,
        )

    def garbage_collect(
        self, *, now_ms: int | None = None, batch_size: int = 500
    ) -> GarbageCollectionResult:
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("gc batch size must be between 1 and 10000")
        if not self.database_path.is_file():
            raise FileNotFoundError(f"runtime database does not exist: {self.database_path}")
        due = int(time.time() * 1000) if now_ms is None else now_ms
        with DataDirectoryLock(self.data_dir), sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            candidate_rows = connection.execute(
                """
                SELECT DISTINCT a.blob_id
                FROM im_attachments a
                LEFT JOIN im_messages m
                  ON m.tenant_key = a.tenant_key
                 AND m.app_id = a.app_id
                 AND m.message_id = a.message_id
                WHERE a.blob_id IS NOT NULL
                  AND (
                    (a.expires_at_ms IS NOT NULL AND a.expires_at_ms <= ?)
                    OR (m.expires_at_ms IS NOT NULL AND m.expires_at_ms <= ?)
                  )
                LIMIT ?
                """,
                (due, due, batch_size * 2),
            ).fetchall()
            candidates = {str(row[0]) for row in candidate_rows}
            connection.execute("BEGIN IMMEDIATE")
            checkpoints = connection.execute(
                """
                DELETE FROM runtime_checkpoints WHERE run_id IN (
                    SELECT DISTINCT s.run_id
                    FROM runtime_checkpoint_sources s
                    JOIN runtime_runs r ON r.run_id = s.run_id
                    WHERE EXISTS (
                        SELECT 1 FROM im_messages m
                        WHERE m.expires_at_ms IS NOT NULL AND m.expires_at_ms <= ?
                          AND r.session_key LIKE m.tenant_key || '/' || m.app_id || '/%'
                          AND (
                            s.source_ref = 'im.message:' || m.message_id
                            OR s.source_ref LIKE 'im.attachment:' || m.message_id || ':%'
                          )
                    ) OR EXISTS (
                        SELECT 1 FROM im_attachments a
                        WHERE a.expires_at_ms IS NOT NULL AND a.expires_at_ms <= ?
                          AND r.session_key LIKE a.tenant_key || '/' || a.app_id || '/%'
                          AND s.source_ref = 'im.attachment:' || a.message_id || ':'
                              || a.resource_key
                    )
                    LIMIT ?
                )
                """,
                (due, due, batch_size),
            ).rowcount
            event_payloads = connection.execute(
                """
                UPDATE runtime_events SET payload_ciphertext = NULL
                WHERE event_pk IN (
                    SELECT event_pk FROM runtime_events
                    WHERE payload_ciphertext IS NOT NULL
                      AND payload_expires_at_ms IS NOT NULL
                      AND payload_expires_at_ms <= ?
                    ORDER BY event_pk LIMIT ?
                )
                """,
                (due, batch_size),
            ).rowcount
            attachments = connection.execute(
                """
                DELETE FROM im_attachments WHERE rowid IN (
                    SELECT rowid FROM im_attachments
                    WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= ?
                    ORDER BY rowid LIMIT ?
                )
                """,
                (due, batch_size),
            ).rowcount
            messages = connection.execute(
                """
                DELETE FROM im_messages WHERE rowid IN (
                    SELECT rowid FROM im_messages
                    WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= ?
                    ORDER BY rowid LIMIT ?
                )
                """,
                (due, batch_size),
            ).rowcount
            artifacts = connection.execute(
                """
                DELETE FROM runtime_artifacts WHERE rowid IN (
                    SELECT rowid FROM runtime_artifacts
                    WHERE expires_at_ms IS NOT NULL AND expires_at_ms <= ?
                    ORDER BY rowid LIMIT ?
                )
                """,
                (due, batch_size),
            ).rowcount
            idempotency = connection.execute(
                """
                DELETE FROM runtime_idempotency WHERE rowid IN (
                    SELECT rowid FROM runtime_idempotency
                    WHERE expires_at_ms <= ? ORDER BY rowid LIMIT ?
                )
                """,
                (due, batch_size),
            ).rowcount
            connection.commit()

            for path in self._bounded_blob_files(batch_size):
                candidates.add(path.stem)
            deleted = 0
            reclaimed = 0
            for blob_id in sorted(candidates)[:batch_size]:
                self._validate_blob_id(blob_id)
                referenced = connection.execute(
                    "SELECT 1 FROM im_attachments WHERE blob_id = ? LIMIT 1", (blob_id,)
                ).fetchone()
                if referenced is not None:
                    continue
                path = self._blob_path(self.blob_root, blob_id)
                if path.is_file():
                    reclaimed += path.stat().st_size
                    path.unlink()
                    deleted += 1
                connection.execute("DELETE FROM im_file_blobs WHERE blob_id = ?", (blob_id,))
            connection.commit()
        return GarbageCollectionResult(
            True,
            event_payloads,
            messages,
            attachments,
            artifacts,
            idempotency,
            checkpoints,
            deleted,
            reclaimed,
        )

    def purge_message(self, *, tenant_key: str, app_id: str, message_id: str) -> PurgeResult:
        return self._purge(
            target_kind="message",
            tenant_key=tenant_key,
            app_id=app_id,
            resource_id=message_id,
        )

    def purge_chat(self, *, tenant_key: str, app_id: str, chat_id: str) -> PurgeResult:
        return self._purge(
            target_kind="chat",
            tenant_key=tenant_key,
            app_id=app_id,
            resource_id=chat_id,
        )

    def purge_tenant(self, *, tenant_key: str) -> PurgeResult:
        if not tenant_key.strip():
            raise ValueError("tenant purge target is required")
        if not self.database_path.is_file():
            raise FileNotFoundError(f"runtime database does not exist: {self.database_path}")
        with DataDirectoryLock(self.data_dir), sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.row_factory = sqlite3.Row
            exists = connection.execute(
                """
                SELECT 1 FROM im_chats WHERE tenant_key = ?
                UNION ALL SELECT 1 FROM runtime_events WHERE tenant_key = ?
                UNION ALL SELECT 1 FROM runtime_graphs WHERE tenant_key = ?
                UNION ALL SELECT 1 FROM runtime_tasks WHERE session_key LIKE ?
                LIMIT 1
                """,
                (tenant_key, tenant_key, tenant_key, f"{tenant_key}/%"),
            ).fetchone()
            if exists is None:
                raise LookupError("exact tenant purge target does not exist")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA defer_foreign_keys=ON")
            try:
                self._prepare_tenant_purge_tables(connection, tenant_key)
                candidates = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT DISTINCT blob_id FROM im_attachments "
                        "WHERE tenant_key = ? AND blob_id IS NOT NULL",
                        (tenant_key,),
                    )
                )
                counts = {
                    "messages": self._temp_count(connection, "purge_messages"),
                    "attachments": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM im_attachments WHERE tenant_key = ?",
                            (tenant_key,),
                        ).fetchone()[0]
                    ),
                    "checkpoints": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM runtime_checkpoints "
                            "WHERE run_id IN (SELECT run_id FROM purge_runs)"
                        ).fetchone()[0]
                    ),
                    "tasks": self._temp_count(connection, "purge_tasks"),
                    "runs": self._temp_count(connection, "purge_runs"),
                    "outbox": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM runtime_outbox "
                            "WHERE task_id IN (SELECT task_id FROM purge_tasks) "
                            "OR run_id IN (SELECT run_id FROM purge_runs)"
                        ).fetchone()[0]
                    ),
                }
                connection.execute(
                    "DELETE FROM runtime_approvals WHERE task_id IN "
                    "(SELECT task_id FROM purge_tasks)"
                )
                connection.execute(
                    "DELETE FROM runtime_run_controls WHERE target_task_id IN "
                    "(SELECT task_id FROM purge_tasks) OR event_pk IN "
                    "(SELECT event_pk FROM purge_events)"
                )
                connection.execute(
                    "DELETE FROM runtime_outbox WHERE task_id IN "
                    "(SELECT task_id FROM purge_tasks) OR run_id IN "
                    "(SELECT run_id FROM purge_runs)"
                )
                connection.execute(
                    "DELETE FROM runtime_idempotency WHERE EXISTS ("
                    "SELECT 1 FROM purge_runs r WHERE owner LIKE r.run_id || ':%')"
                )
                connection.execute(
                    "DELETE FROM runtime_run_events WHERE run_id IN (SELECT run_id FROM purge_runs)"
                )
                connection.execute(
                    "DELETE FROM runtime_checkpoints WHERE run_id IN "
                    "(SELECT run_id FROM purge_runs)"
                )
                connection.execute("DELETE FROM runtime_graphs WHERE tenant_key = ?", (tenant_key,))
                connection.execute(
                    "DELETE FROM runtime_runs WHERE run_id IN (SELECT run_id FROM purge_runs)"
                )
                connection.execute(
                    "DELETE FROM runtime_tasks WHERE task_id IN (SELECT task_id FROM purge_tasks)"
                )
                connection.execute(
                    "DELETE FROM runtime_events WHERE event_pk IN "
                    "(SELECT event_pk FROM purge_events)"
                )
                attachments = connection.execute(
                    "DELETE FROM im_attachments WHERE tenant_key = ?", (tenant_key,)
                ).rowcount
                messages = connection.execute(
                    "DELETE FROM im_messages WHERE tenant_key = ?", (tenant_key,)
                ).rowcount
                connection.execute("DELETE FROM im_chats WHERE tenant_key = ?", (tenant_key,))
                unreferenced = self._drop_blob_metadata(connection, candidates)
                counts["blobs"] = len(unreferenced)
                self._insert_purge_audit(
                    connection,
                    target_kind="tenant",
                    target_digest=hashlib.sha256(tenant_key.encode()).hexdigest(),
                    counts=counts,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            deleted, reclaimed = self._delete_blob_ids(unreferenced)
            return PurgeResult(
                True,
                "tenant",
                messages,
                attachments,
                counts["checkpoints"],
                counts["tasks"],
                counts["runs"],
                counts["outbox"],
                deleted,
                reclaimed,
            )

    def purge_all(self) -> PurgeResult:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"runtime database does not exist: {self.database_path}")
        blob_paths = self._bounded_blob_files(10_000_000)
        for path in blob_paths:
            self._validate_blob_id(path.stem)
        with DataDirectoryLock(self.data_dir), sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA defer_foreign_keys=ON")
            try:
                business_tables = (
                    "runtime_plugins",
                    "runtime_events",
                    "runtime_tasks",
                    "runtime_runs",
                    "runtime_run_events",
                    "runtime_outbox",
                    "runtime_idempotency",
                    "runtime_checkpoints",
                    "runtime_graphs",
                    "runtime_agent_nodes",
                    "runtime_agent_edges",
                    "runtime_mailbox",
                    "runtime_artifacts",
                    "runtime_agent_checkpoints",
                    "runtime_resource_locks",
                    "runtime_budget_ledger",
                    "runtime_run_controls",
                    "runtime_checkpoint_sources",
                    "runtime_approvals",
                    "im_chats",
                    "im_messages",
                    "im_attachments",
                    "im_file_blobs",
                )
                counts = {
                    table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in business_tables
                }
                counts["blobs"] = len(blob_paths)
                messages = counts["im_messages"]
                attachments = counts["im_attachments"]
                checkpoints = counts["runtime_checkpoints"] + counts["runtime_agent_checkpoints"]
                tasks = counts["runtime_tasks"]
                runs = counts["runtime_runs"]
                outbox = counts["runtime_outbox"]
                connection.execute("DELETE FROM runtime_admin_audit")
                for table in reversed(business_tables):
                    connection.execute(f"DELETE FROM {table}")
                self._insert_purge_audit(
                    connection,
                    target_kind="all",
                    target_digest=hashlib.sha256(b"all-business-data-v1").hexdigest(),
                    counts=counts,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        deleted = 0
        reclaimed = 0
        for path in blob_paths:
            if path.is_file():
                reclaimed += path.stat().st_size
                path.unlink()
                deleted += 1
        return PurgeResult(
            True,
            "all",
            messages,
            attachments,
            checkpoints,
            tasks,
            runs,
            outbox,
            deleted,
            reclaimed,
        )

    def _purge(
        self, *, target_kind: str, tenant_key: str, app_id: str, resource_id: str
    ) -> PurgeResult:
        if target_kind not in {"message", "chat"}:
            raise ValueError("unsupported purge target")
        if not all(value.strip() for value in (tenant_key, app_id, resource_id)):
            raise ValueError("purge target identifiers are required")
        if not self.database_path.is_file():
            raise FileNotFoundError(f"runtime database does not exist: {self.database_path}")
        with DataDirectoryLock(self.data_dir), sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.row_factory = sqlite3.Row
            target_column = "message_id" if target_kind == "message" else "chat_id"
            target = connection.execute(
                f"""
                SELECT 1 FROM im_{"messages" if target_kind == "message" else "chats"}
                WHERE tenant_key = ? AND app_id = ? AND {target_column} = ?
                """,
                (tenant_key, app_id, resource_id),
            ).fetchone()
            if target is None:
                raise LookupError(f"exact {target_kind} purge target does not exist")
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._prepare_purge_tables(
                    connection,
                    target_kind=target_kind,
                    tenant_key=tenant_key,
                    app_id=app_id,
                    resource_id=resource_id,
                )
                candidates = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT blob_id FROM im_attachments
                        WHERE blob_id IS NOT NULL AND message_id IN (
                            SELECT message_id FROM purge_messages
                        ) AND tenant_key = ? AND app_id = ?
                        """,
                        (tenant_key, app_id),
                    )
                )
                checkpoints = connection.execute(
                    """
                    DELETE FROM runtime_checkpoints WHERE run_id IN (
                        SELECT run_id FROM purge_runs
                    ) OR run_id IN (
                        SELECT s.run_id FROM runtime_checkpoint_sources s
                        JOIN purge_messages m
                          ON s.source_ref = 'im.message:' || m.message_id
                          OR s.source_ref LIKE 'im.attachment:' || m.message_id || ':%'
                    )
                    """
                ).rowcount
                connection.execute(
                    "DELETE FROM runtime_run_controls WHERE target_task_id IN "
                    "(SELECT task_id FROM purge_tasks) OR event_pk IN "
                    "(SELECT event_pk FROM purge_events)"
                )
                outbox = connection.execute(
                    """
                    DELETE FROM runtime_outbox
                    WHERE task_id IN (SELECT task_id FROM purge_tasks)
                       OR run_id IN (SELECT run_id FROM purge_runs)
                    """
                ).rowcount
                connection.execute(
                    "DELETE FROM runtime_run_events WHERE run_id IN (SELECT run_id FROM purge_runs)"
                )
                connection.execute(
                    "DELETE FROM runtime_graphs WHERE root_run_id IN "
                    "(SELECT run_id FROM purge_runs)"
                )
                runs = connection.execute(
                    "DELETE FROM runtime_runs WHERE run_id IN (SELECT run_id FROM purge_runs)"
                ).rowcount
                tasks = connection.execute(
                    "DELETE FROM runtime_tasks WHERE task_id IN (SELECT task_id FROM purge_tasks)"
                ).rowcount
                connection.execute(
                    "DELETE FROM runtime_events WHERE event_pk IN "
                    "(SELECT event_pk FROM purge_events)"
                )
                attachments = connection.execute(
                    """
                    DELETE FROM im_attachments
                    WHERE tenant_key = ? AND app_id = ?
                      AND message_id IN (SELECT message_id FROM purge_messages)
                    """,
                    (tenant_key, app_id),
                ).rowcount
                messages = connection.execute(
                    """
                    DELETE FROM im_messages
                    WHERE tenant_key = ? AND app_id = ?
                      AND message_id IN (SELECT message_id FROM purge_messages)
                    """,
                    (tenant_key, app_id),
                ).rowcount
                if target_kind == "chat":
                    connection.execute(
                        """
                        DELETE FROM im_chats
                        WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                        """,
                        (tenant_key, app_id, resource_id),
                    )
                unreferenced = self._drop_blob_metadata(connection, candidates)
                counts = {
                    "messages": messages,
                    "attachments": attachments,
                    "checkpoints": checkpoints,
                    "tasks": tasks,
                    "runs": runs,
                    "outbox": outbox,
                    "blobs": len(unreferenced),
                }
                connection.execute(
                    """
                    INSERT INTO runtime_admin_audit(
                        operation, target_kind, target_digest, result_counts, created_at_ms
                    ) VALUES ('purge', ?, ?, ?, ?)
                    """,
                    (
                        target_kind,
                        hashlib.sha256(f"{tenant_key}:{app_id}:{resource_id}".encode()).hexdigest(),
                        json.dumps(counts, sort_keys=True, separators=(",", ":")),
                        int(time.time() * 1000),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            deleted = 0
            reclaimed = 0
            for blob_id in unreferenced:
                path = self._blob_path(self.blob_root, blob_id)
                if path.is_file():
                    reclaimed += path.stat().st_size
                    path.unlink()
                    deleted += 1
            return PurgeResult(
                True,
                target_kind,
                messages,
                attachments,
                checkpoints,
                tasks,
                runs,
                outbox,
                deleted,
                reclaimed,
            )

    @staticmethod
    def _prepare_purge_tables(
        connection: sqlite3.Connection,
        *,
        target_kind: str,
        tenant_key: str,
        app_id: str,
        resource_id: str,
    ) -> None:
        for name in ("purge_messages", "purge_tasks", "purge_runs", "purge_events"):
            connection.execute(f"DROP TABLE IF EXISTS temp.{name}")
        connection.execute("CREATE TEMP TABLE purge_messages(message_id TEXT PRIMARY KEY)")
        if target_kind == "message":
            connection.execute("INSERT INTO purge_messages VALUES (?)", (resource_id,))
        else:
            connection.execute(
                """
                INSERT INTO purge_messages
                SELECT message_id FROM im_messages
                WHERE tenant_key = ? AND app_id = ? AND chat_id = ?
                """,
                (tenant_key, app_id, resource_id),
            )
        connection.execute("CREATE TEMP TABLE purge_tasks(task_id TEXT PRIMARY KEY)")
        if target_kind == "message":
            connection.execute(
                """
                INSERT INTO purge_tasks
                SELECT t.task_id FROM runtime_tasks t JOIN runtime_events e
                  ON e.event_pk = t.event_pk
                WHERE e.tenant_key = ? AND e.app_id = ?
                  AND e.resource_kind = 'im.message' AND e.resource_id = ?
                """,
                (tenant_key, app_id, resource_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO purge_tasks
                SELECT task_id FROM runtime_tasks WHERE session_key LIKE ?
                """,
                (f"{tenant_key}/{app_id}/{resource_id}/%",),
            )
        connection.execute("CREATE TEMP TABLE purge_runs(run_id TEXT PRIMARY KEY)")
        connection.execute(
            """
            INSERT INTO purge_runs
            SELECT run_id FROM runtime_runs
            WHERE task_id IN (SELECT task_id FROM purge_tasks)
            """
        )
        connection.execute("CREATE TEMP TABLE purge_events(event_pk INTEGER PRIMARY KEY)")
        connection.execute(
            """
            INSERT INTO purge_events
            SELECT event_pk FROM runtime_tasks
            WHERE task_id IN (SELECT task_id FROM purge_tasks) AND event_pk IS NOT NULL
            """
        )
        if target_kind == "chat":
            connection.execute(
                """
                INSERT OR IGNORE INTO purge_events
                SELECT event_pk FROM runtime_events
                WHERE tenant_key = ? AND app_id = ?
                  AND resource_kind = 'im.chat' AND resource_id = ?
                """,
                (tenant_key, app_id, resource_id),
            )

    @staticmethod
    def _prepare_tenant_purge_tables(connection: sqlite3.Connection, tenant_key: str) -> None:
        for name in (
            "purge_messages",
            "purge_tasks",
            "purge_runs",
            "purge_events",
        ):
            connection.execute(f"DROP TABLE IF EXISTS temp.{name}")
        connection.execute("CREATE TEMP TABLE purge_messages(message_id TEXT PRIMARY KEY)")
        connection.execute(
            "INSERT INTO purge_messages SELECT message_id FROM im_messages WHERE tenant_key = ?",
            (tenant_key,),
        )
        connection.execute("CREATE TEMP TABLE purge_events(event_pk INTEGER PRIMARY KEY)")
        connection.execute(
            "INSERT INTO purge_events SELECT event_pk FROM runtime_events WHERE tenant_key = ?",
            (tenant_key,),
        )
        connection.execute("CREATE TEMP TABLE purge_tasks(task_id TEXT PRIMARY KEY)")
        connection.execute(
            """
            INSERT INTO purge_tasks
            SELECT task_id FROM runtime_tasks
            WHERE event_pk IN (SELECT event_pk FROM purge_events)
               OR session_key LIKE ?
            """,
            (f"{tenant_key}/%",),
        )
        connection.execute("CREATE TEMP TABLE purge_runs(run_id TEXT PRIMARY KEY)")
        connection.execute(
            "INSERT INTO purge_runs SELECT run_id FROM runtime_runs "
            "WHERE task_id IN (SELECT task_id FROM purge_tasks)"
        )

    @staticmethod
    def _temp_count(connection: sqlite3.Connection, table: str) -> int:
        if table not in {"purge_messages", "purge_tasks", "purge_runs", "purge_events"}:
            raise ValueError("unsupported purge count table")
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    @staticmethod
    def _insert_purge_audit(
        connection: sqlite3.Connection,
        *,
        target_kind: str,
        target_digest: str,
        counts: dict[str, int],
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_admin_audit(
                operation, target_kind, target_digest, result_counts, created_at_ms
            ) VALUES ('purge', ?, ?, ?, ?)
            """,
            (
                target_kind,
                target_digest,
                json.dumps(counts, sort_keys=True, separators=(",", ":")),
                int(time.time() * 1000),
            ),
        )

    def _delete_blob_ids(self, blob_ids: tuple[str, ...]) -> tuple[int, int]:
        deleted = 0
        reclaimed = 0
        for blob_id in blob_ids:
            self._validate_blob_id(blob_id)
            path = self._blob_path(self.blob_root, blob_id)
            if path.is_file():
                reclaimed += path.stat().st_size
                path.unlink()
                deleted += 1
        return deleted, reclaimed

    @staticmethod
    def _drop_blob_metadata(
        connection: sqlite3.Connection, candidates: tuple[str, ...]
    ) -> tuple[str, ...]:
        result: list[str] = []
        for blob_id in candidates:
            if (
                connection.execute(
                    "SELECT 1 FROM im_attachments WHERE blob_id = ? LIMIT 1", (blob_id,)
                ).fetchone()
                is None
            ):
                connection.execute("DELETE FROM im_file_blobs WHERE blob_id = ?", (blob_id,))
                result.append(blob_id)
        return tuple(result)

    @classmethod
    def verify_backup(cls, archive: Path) -> BackupResult:
        archive = cls._absolute(archive, "backup archive")
        manifest = cls._verified_manifest(archive)
        cls._verify_archived_database(archive)
        files = manifest["files"]
        return BackupResult(
            True,
            str(archive),
            int(manifest["schema_version"]),
            len(files),
            sum(int(value["bytes"]) for value in files.values()),
        )

    @classmethod
    def restore(cls, archive: Path, target: Path) -> BackupResult:
        archive = cls._absolute(archive, "backup archive")
        target = cls._absolute(target, "restore data directory")
        manifest = cls._verified_manifest(archive)
        with DataDirectoryLock(target):
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise FileExistsError("restore data directory must be new or empty")
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".codex2lark-restore-", dir=target.parent))
            try:
                with zipfile.ZipFile(archive) as bundle:
                    for name in manifest["files"]:
                        destination = staging.joinpath(*PurePosixPath(name).parts)
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        with bundle.open(name) as source, destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        os.chmod(destination, 0o600)
                with cls._readonly(staging / "runtime.db") as connection:
                    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                    if integrity != "ok":
                        raise RuntimeError(f"restored database integrity failed: {integrity}")
                os.chmod(staging, 0o700)
                if target.exists():
                    target.rmdir()
                os.replace(staging, target)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return BackupResult(
            True,
            str(target),
            int(manifest["schema_version"]),
            len(manifest["files"]),
            sum(int(value["bytes"]) for value in manifest["files"].values()),
        )

    @staticmethod
    def as_json(
        result: (
            StorageStatus | BackupResult | GarbageCollectionResult | PurgeResult | KeyRotationResult
        ),
    ) -> str:
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _absolute(path: Path, label: str) -> Path:
        if not path.is_absolute():
            raise ValueError(f"{label} must be an absolute path")
        return path.resolve()

    @staticmethod
    def _readonly(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _schema_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT MAX(version) FROM runtime_migrations").fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _state_counts(connection: sqlite3.Connection, table: str) -> dict[str, int]:
        state_columns = {
            "runtime_tasks": "state",
            "runtime_outbox": "state",
            "runtime_runs": "status",
            "runtime_graphs": "status",
            "runtime_agent_nodes": "status",
            "runtime_approvals": "state",
        }
        column = state_columns.get(table)
        if column is None:
            raise ValueError("unsupported state table")
        return {
            str(row["lifecycle_state"]): int(row["count"])
            for row in connection.execute(
                f"SELECT {column} AS lifecycle_state, COUNT(*) AS count "
                f"FROM {table} GROUP BY {column}"
            )
        }

    @staticmethod
    def _sum_attempts(connection: sqlite3.Connection, table: str) -> int:
        if table not in {"runtime_tasks", "runtime_outbox"}:
            raise ValueError("unsupported retry table")
        row = connection.execute(f"SELECT COALESCE(SUM(attempt_count), 0) FROM {table}").fetchone()
        return int(row[0])

    @staticmethod
    def _oldest_pending_age(connection: sqlite3.Connection, table: str, now_ms: int) -> int:
        if table not in {"runtime_tasks", "runtime_outbox"}:
            raise ValueError("unsupported pending-age table")
        row = connection.execute(
            f"SELECT MIN(created_at_ms) FROM {table} WHERE state = 'pending'"
        ).fetchone()
        created_at_ms = row[0]
        return max(0, now_ms - int(created_at_ms)) if created_at_ms is not None else 0

    @staticmethod
    def _referenced_blob_ids(connection: sqlite3.Connection) -> tuple[str, ...]:
        rows = connection.execute(
            "SELECT DISTINCT blob_id FROM im_attachments WHERE blob_id IS NOT NULL"
        ).fetchall()
        values = tuple(sorted(str(row[0]) for row in rows))
        for value in values:
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise RuntimeError("database contains an invalid blob identifier")
        return values

    @staticmethod
    def _blob_path(root: Path, blob_id: str) -> Path:
        return root / blob_id[:2] / f"{blob_id}.blob"

    def _bounded_blob_files(self, limit: int) -> tuple[Path, ...]:
        if not self.blob_root.is_dir():
            return ()
        values: list[Path] = []
        for parent in sorted(self.blob_root.iterdir()):
            if not parent.is_dir():
                continue
            for path in sorted(parent.glob("*.blob")):
                values.append(path)
                if len(values) >= limit:
                    return tuple(values)
        return tuple(values)

    @staticmethod
    def _validate_blob_id(blob_id: str) -> None:
        if len(blob_id) != 64 or any(character not in "0123456789abcdef" for character in blob_id):
            raise RuntimeError("database or blob directory contains an invalid blob identifier")

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _verified_manifest(cls, archive: Path) -> dict[str, Any]:
        if not archive.is_file():
            raise FileNotFoundError(f"backup archive does not exist: {archive}")
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise RuntimeError("backup archive has duplicate entries or no manifest")
            manifest = json.loads(bundle.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != "codex2lark-backup-v1":
                raise RuntimeError("unsupported backup manifest")
            schema_version = manifest.get("schema_version")
            files = manifest.get("files")
            if not isinstance(schema_version, int) or schema_version > SCHEMA_VERSION:
                raise RuntimeError("backup schema is newer than this Codex2Lark version")
            if not isinstance(files, dict) or set(names) != {"manifest.json", *files}:
                raise RuntimeError("backup archive entries do not match the manifest")
            if "runtime.db" not in files:
                raise RuntimeError("backup manifest has no runtime database")
            for name, expected in files.items():
                cls._validate_archive_name(name)
                if not isinstance(expected, dict):
                    raise RuntimeError("backup file metadata is invalid")
                digest = hashlib.sha256()
                size = 0
                with bundle.open(name) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                if size != expected.get("bytes") or digest.hexdigest() != expected.get("sha256"):
                    raise RuntimeError(f"backup file verification failed: {name}")
            return manifest

    @staticmethod
    def _validate_archive_name(name: str) -> None:
        path = PurePosixPath(name)
        allowed_blob = (
            len(path.parts) == 3
            and path.parts[0] == "blobs"
            and len(path.parts[1]) == 2
            and path.parts[2].endswith(".blob")
            and path.parts[2][:-5].startswith(path.parts[1])
        )
        if path.is_absolute() or ".." in path.parts or (name != "runtime.db" and not allowed_blob):
            raise RuntimeError(f"unsafe or unsupported backup path: {name}")

    @classmethod
    def _verify_archived_database(cls, archive: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="codex2lark-verify-") as raw:
            snapshot = Path(raw) / "runtime.db"
            with (
                zipfile.ZipFile(archive) as bundle,
                bundle.open("runtime.db") as source,
                snapshot.open("wb") as output,
            ):
                shutil.copyfileobj(source, output)
            with cls._readonly(snapshot) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"backup database integrity failed: {integrity}")
