from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LiveGroupResult:
    chat_id: str
    observed: bool
    message_id: str | None = None
    graph_status: str | None = None
    task_state: str | None = None
    acknowledgement_sent: bool = False
    terminal_sent_count: int = 0

    @property
    def ok(self) -> bool:
        return (
            self.observed
            and self.graph_status == "completed"
            and self.task_state == "succeeded"
            and self.acknowledgement_sent
            and self.terminal_sent_count == 1
        )


@dataclass(frozen=True, slots=True)
class LiveMultiGroupResult:
    ok: bool
    since_ms: int
    groups: tuple[LiveGroupResult, ...]
    timed_out: bool = False


class LiveMultiGroupAcceptance:
    _TERMINAL_KINDS = ("completed", "blocked", "failed", "cancelled")

    def __init__(self, data_dir: Path) -> None:
        if not data_dir.is_absolute():
            raise ValueError("data directory must be absolute")
        self.database_path = data_dir.resolve() / "runtime.db"

    def snapshot(self, chat_ids: tuple[str, ...], *, since_ms: int) -> LiveMultiGroupResult:
        selected = self._validate(chat_ids, since_ms)
        if not self.database_path.is_file():
            raise FileNotFoundError(f"runtime database does not exist: {self.database_path}")
        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            groups = tuple(
                self._group_snapshot(connection, chat_id, since_ms) for chat_id in selected
            )
        finally:
            connection.close()
        return LiveMultiGroupResult(all(group.ok for group in groups), since_ms, groups)

    def wait(
        self,
        chat_ids: tuple[str, ...],
        *,
        since_ms: int,
        timeout_seconds: float,
        poll_seconds: float = 1.0,
    ) -> LiveMultiGroupResult:
        if timeout_seconds < 0 or poll_seconds <= 0:
            raise ValueError("acceptance timeout and poll interval are invalid")
        deadline = time.monotonic() + timeout_seconds
        while True:
            result = self.snapshot(chat_ids, since_ms=since_ms)
            if result.ok:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return LiveMultiGroupResult(False, since_ms, result.groups, timed_out=True)
            time.sleep(min(poll_seconds, remaining))

    @staticmethod
    def as_json(result: LiveMultiGroupResult) -> str:
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)

    @classmethod
    def _group_snapshot(
        cls, connection: sqlite3.Connection, chat_id: str, since_ms: int
    ) -> LiveGroupResult:
        row = connection.execute(
            """
            SELECT m.message_id, g.status AS graph_status, t.state AS task_state, t.task_id
            FROM im_messages m
            JOIN runtime_graphs g
              ON g.tenant_key = m.tenant_key
             AND g.app_id = m.app_id
             AND g.source_resource_kind = 'im.message'
             AND g.source_resource_id = m.message_id
            JOIN runtime_runs r ON r.run_id = g.root_run_id
            JOIN runtime_tasks t ON t.task_id = r.task_id
            WHERE m.chat_id = ? AND g.created_at_ms >= ?
            ORDER BY g.created_at_ms, g.graph_id
            LIMIT 1
            """,
            (chat_id, since_ms),
        ).fetchone()
        if row is None:
            return LiveGroupResult(chat_id, False)
        counts = {
            str(item["message_kind"]): int(item["count"])
            for item in connection.execute(
                """
                SELECT message_kind, COUNT(*) AS count
                FROM runtime_outbox
                WHERE task_id = ? AND state = 'sent'
                GROUP BY message_kind
                """,
                (row["task_id"],),
            )
        }
        return LiveGroupResult(
            chat_id=chat_id,
            observed=True,
            message_id=str(row["message_id"]),
            graph_status=str(row["graph_status"]),
            task_state=str(row["task_state"]),
            acknowledgement_sent=counts.get("acknowledgement", 0) == 1,
            terminal_sent_count=sum(counts.get(kind, 0) for kind in cls._TERMINAL_KINDS),
        )

    @staticmethod
    def _validate(chat_ids: tuple[str, ...], since_ms: int) -> tuple[str, ...]:
        selected = tuple(dict.fromkeys(chat_id.strip() for chat_id in chat_ids if chat_id.strip()))
        if len(selected) < 2:
            raise ValueError("live multi-group acceptance requires at least two distinct chat IDs")
        if since_ms < 0:
            raise ValueError("since-ms cannot be negative")
        return selected
