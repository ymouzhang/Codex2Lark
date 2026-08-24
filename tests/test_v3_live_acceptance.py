from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.live_acceptance import LiveMultiGroupAcceptance


async def initialized(path: Path) -> None:
    database = SQLiteDatabase(path / "runtime.db")
    await database.open()
    await database.close()


def insert_group(
    connection: sqlite3.Connection,
    chat_id: str,
    suffix: str,
    *,
    terminal_count: int = 1,
) -> None:
    task_id = f"task-{suffix}"
    run_id = f"run-{suffix}"
    message_id = f"message-{suffix}"
    connection.execute(
        "INSERT INTO im_chats VALUES (?, 'app', ?, NULL, 'group', 1, 'joined', "
        "'available', 1000, 'default', NULL)",
        ("tenant", chat_id),
    )
    connection.execute(
        """
        INSERT INTO im_messages VALUES (
            'tenant', 'app', ?, ?, NULL, NULL, NULL, 'user', 'actor', NULL,
            'text', X'01', X'01', 'hash', 1000, 1000, 0, 0, 1, 1000, NULL
        )
        """,
        (message_id, chat_id),
    )
    connection.execute(
        """
        INSERT INTO runtime_tasks(
            task_id, plugin_id, command_type, session_key, priority,
            payload_ciphertext, state, available_at_ms, attempt_count,
            max_attempts, created_at_ms, updated_at_ms
        ) VALUES (?, 'im', 'handle', ?, 0, X'01', 'succeeded', 1000, 1, 5, 1000, 1000)
        """,
        (task_id, chat_id),
    )
    connection.execute(
        "INSERT INTO runtime_runs VALUES (?, ?, ?, 'root', 1, 1, 'completed', 1000, 1000)",
        (run_id, task_id, chat_id),
    )
    connection.execute(
        """
        INSERT INTO runtime_graphs VALUES (
            ?, ?, ?, 'tenant', 'app', 'im.message', ?, 'root', 1,
            'completed', 3, 8, 4, 1000, 1000
        )
        """,
        (f"graph-{suffix}", run_id, f"node-{suffix}", message_id),
    )

    def outbox(kind: str, index: int) -> None:
        connection.execute(
            """
            INSERT INTO runtime_outbox(
                outbox_id, run_id, task_id, publisher_id, destination_ref,
                message_kind, idempotency_key, payload_ciphertext, state,
                available_at_ms, attempt_count, max_attempts, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, 'feishu-im.reply', ?, ?, ?, X'01', 'sent', 1000, 1, 8, 1000, 1000)
            """,
            (
                f"outbox-{suffix}-{kind}-{index}",
                run_id,
                task_id,
                message_id,
                kind,
                f"key-{suffix}-{kind}-{index}",
            ),
        )

    outbox("acknowledgement", 0)
    for index in range(terminal_count):
        outbox("completed", index)


async def test_live_multigroup_acceptance_observes_only_lifecycle_state(tmp_path: Path) -> None:
    data_dir = tmp_path.resolve()
    await initialized(data_dir)
    with sqlite3.connect(data_dir / "runtime.db") as connection:
        insert_group(connection, "oc_first", "first")
        insert_group(connection, "oc_second", "second")
        connection.commit()

    observer = LiveMultiGroupAcceptance(data_dir)
    result = observer.snapshot(("oc_first", "oc_second"), since_ms=900)
    encoded = observer.as_json(result)

    assert result.ok
    assert all(group.ok for group in result.groups)
    assert "payload" not in encoded and "content" not in encoded


async def test_live_multigroup_acceptance_rejects_duplicate_terminal_and_times_out(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path.resolve()
    await initialized(data_dir)
    with sqlite3.connect(data_dir / "runtime.db") as connection:
        insert_group(connection, "oc_first", "first", terminal_count=2)
        insert_group(connection, "oc_second", "second")
        connection.commit()

    observer = LiveMultiGroupAcceptance(data_dir)
    result = observer.wait(("oc_first", "oc_second"), since_ms=900, timeout_seconds=0)

    assert not result.ok and result.timed_out
    assert result.groups[0].terminal_sent_count == 2


async def test_live_multigroup_acceptance_requires_distinct_groups(tmp_path: Path) -> None:
    data_dir = tmp_path.resolve()
    await initialized(data_dir)
    observer = LiveMultiGroupAcceptance(data_dir)

    with pytest.raises(ValueError, match="at least two"):
        observer.snapshot(("oc_one", "oc_one"), since_ms=0)
