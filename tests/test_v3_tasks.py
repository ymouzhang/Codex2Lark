from __future__ import annotations

import asyncio
from pathlib import Path

from codex2lark.core.events import (
    LeasedTask,
    NormalizedEvent,
    OutboxDraft,
    TaskCommand,
    TaskState,
)
from codex2lark.runtime.tasks import (
    DurableTaskWorker,
    TaskExecutionResult,
)
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.runtime_store import RuntimeStore


def event(index: int) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"event-{index}",
        plugin_id="feishu-im",
        event_type="im.message.receive_v1",
        tenant_key="tenant-1",
        app_id="app-1",
        occurred_at_ms=index,
        received_at_ms=index,
        resource_kind="im.message",
        resource_id=f"message-{index}",
        trace_id=f"trace-{index}",
    )


def command(session_key: str, *, max_attempts: int = 3) -> TaskCommand:
    return TaskCommand(
        plugin_id="feishu-im",
        command_type="im.handle_mention",
        session_key=session_key,
        payload={"message_id": session_key},
        max_attempts=max_attempts,
    )


async def setup(tmp_path: Path) -> tuple[SQLiteDatabase, RuntimeStore]:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = RuntimeStore(database, EnvelopeCipher(MasterKey("test", b"t" * 32)))
    return database, store


async def test_task_leasing_serializes_session_and_leases_independent_sessions(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    try:
        await store.admit(event(1), command("session-a"), now_ms=1)
        await store.admit(event(2), command("session-a"), now_ms=2)
        await store.admit(event(3), command("session-b"), now_ms=3)

        leased = await store.lease_tasks(worker_id="worker", now_ms=3, lease_ms=100, limit=3)
        assert {task.session_key for task in leased} == {"session-a", "session-b"}
        assert len(leased) == 2
        for task in leased:
            await store.finish_task(
                task.task_id,
                worker_id="worker",
                state=TaskState.SUCCEEDED,
                now_ms=4,
            )
        remaining = await store.lease_tasks(worker_id="worker", now_ms=5, lease_ms=100, limit=3)
        assert [task.session_key for task in remaining] == ["session-a"]
    finally:
        await database.close()


class ConcurrentHandler:
    def __init__(self, expected: int, *, fail: bool = False) -> None:
        self.expected = expected
        self.fail = fail
        self.active = 0
        self.maximum_active = 0
        self.started = asyncio.Event()

    async def execute(self, task: LeasedTask, *, now_ms: int) -> TaskExecutionResult:
        del task, now_ms
        if self.fail:
            raise ConnectionError("provider unavailable")
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == self.expected:
            self.started.set()
        await asyncio.wait_for(self.started.wait(), timeout=1)
        self.active -= 1
        return TaskExecutionResult(TaskState.SUCCEEDED)

    def failure(self, task: LeasedTask, error: BaseException) -> TaskExecutionResult:
        return TaskExecutionResult(
            TaskState.FAILED,
            OutboxDraft(
                publisher_id="feishu-im.reply",
                destination_ref=str(task.payload["message_id"]),
                message_kind="failed",
                idempotency_key=f"{task.task_id}:terminal",
                payload={"text": f"failed: {type(error).__name__}"},
            ),
            "provider_failed",
        )


async def test_task_worker_runs_independent_sessions_concurrently(tmp_path: Path) -> None:
    database, store = await setup(tmp_path)
    try:
        await store.admit(event(1), command("session-a"), now_ms=1)
        await store.admit(event(2), command("session-b"), now_ms=2)
        handler = ConcurrentHandler(2)
        worker = DurableTaskWorker(
            store,
            {"im.handle_mention": handler},
            worker_id="worker",
            concurrency=2,
        )

        batch = await worker.run_once(now_ms=3)

        assert len(batch.terminal_task_ids) == 2
        assert handler.maximum_active == 2
    finally:
        await database.close()


async def test_task_worker_commits_terminal_failure_when_retries_are_exhausted(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    try:
        await store.admit(event(1), command("session-a", max_attempts=1), now_ms=1)
        worker = DurableTaskWorker(
            store,
            {"im.handle_mention": ConcurrentHandler(1, fail=True)},
            worker_id="worker",
            clock_ms=lambda: 50,
        )

        batch = await worker.run_once(now_ms=2)

        assert len(batch.terminal_task_ids) == 1
        states = await database.call(
            lambda connection: (
                connection.execute("SELECT state FROM runtime_tasks").fetchone()[0],
                connection.execute("SELECT message_kind FROM runtime_outbox").fetchone()[0],
                connection.execute("SELECT updated_at_ms FROM runtime_tasks").fetchone()[0],
                connection.execute("SELECT created_at_ms FROM runtime_outbox").fetchone()[0],
            )
        )
        assert states == ("failed", "failed", 50, 50)
    finally:
        await database.close()


async def test_expired_final_lease_is_terminalized_without_reexecution(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    try:
        await store.admit(event(1), command("session-a", max_attempts=1), now_ms=1)
        abandoned = await store.lease_tasks(worker_id="crashed-worker", now_ms=2, lease_ms=10)
        assert abandoned[0].attempt_count == 1
        handler = ConcurrentHandler(1)
        worker = DurableTaskWorker(
            store,
            {"im.handle_mention": handler},
            worker_id="recovery-worker",
        )

        batch = await worker.run_once(now_ms=12)

        assert len(batch.terminal_task_ids) == 1
        assert handler.maximum_active == 0
        state = await database.call(
            lambda connection: connection.execute(
                "SELECT state, attempt_count, last_error_code FROM runtime_tasks"
            ).fetchone()
        )
        assert tuple(state) == ("failed", 1, "provider_failed")
        outbox = await database.call(
            lambda connection: connection.execute(
                "SELECT message_kind FROM runtime_outbox"
            ).fetchone()[0]
        )
        assert outbox == "failed"
    finally:
        await database.close()
