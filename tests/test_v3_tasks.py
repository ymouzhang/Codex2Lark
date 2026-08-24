from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex2lark.core.events import (
    LeasedTask,
    NormalizedEvent,
    OutboxDraft,
    TaskCommand,
    TaskState,
)
from codex2lark.core.scheduling import TaskConcurrencyLimits
from codex2lark.runtime.multi_agent import (
    AgentRole,
    MultiAgentSupervisor,
    NodeSpec,
)
from codex2lark.runtime.tasks import (
    DurableTaskWorker,
    TaskDeferred,
    TaskExecutionResult,
)
from codex2lark.runtime.types import RunStatus
from codex2lark.storage.agent_store import SQLiteAgentGraphStore
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.runtime_store import RuntimeStore


def event(index: int, *, tenant_key: str = "tenant-1", app_id: str = "app-1") -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"event-{index}",
        plugin_id="feishu-im",
        event_type="im.message.receive_v1",
        tenant_key=tenant_key,
        app_id=app_id,
        occurred_at_ms=index,
        received_at_ms=index,
        resource_kind="im.message",
        resource_id=f"message-{index}",
        trace_id=f"trace-{index}",
    )


def command(session_key: str, *, max_attempts: int = 3, group_id: str | None = None) -> TaskCommand:
    return TaskCommand(
        plugin_id="feishu-im",
        command_type="im.handle_mention",
        session_key=session_key,
        payload={"message_id": session_key},
        max_attempts=max_attempts,
        group_id=group_id,
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
        assert all(task.tenant_key == "tenant-1" for task in leased)
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


async def test_hierarchical_scheduler_serves_quiet_tenant_before_noisy_backlog(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    limits = TaskConcurrencyLimits(2, 2, 2, 2)
    try:
        await store.admit(
            event(1, tenant_key="tenant-a"),
            command("tenant-a/app-1/group-a/thread-1", group_id="group-a"),
            now_ms=1,
        )
        await store.admit(
            event(2, tenant_key="tenant-a"),
            command("tenant-a/app-1/group-a/thread-2", group_id="group-a"),
            now_ms=2,
        )
        await store.admit(
            event(3, tenant_key="tenant-b"),
            command("tenant-b/app-1/group-b/thread-1", group_id="group-b"),
            now_ms=3,
        )

        leased = await store.lease_tasks(
            worker_id="worker",
            now_ms=3,
            lease_ms=100,
            limit=2,
            limits=limits,
        )

        assert [(task.tenant_key, task.group_id) for task in leased] == [
            ("tenant-a", "group-a"),
            ("tenant-b", "group-b"),
        ]
    finally:
        await database.close()


async def test_hierarchical_scheduler_enforces_each_parent_scope_incrementally(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    limits = TaskConcurrencyLimits(4, 2, 1, 1)
    scopes = (
        (1, "tenant-a", "app-a", "group-a", "thread-1"),
        (2, "tenant-a", "app-a", "group-a", "thread-2"),
        (3, "tenant-a", "app-b", "group-b", "thread-1"),
        (4, "tenant-a", "app-c", "group-c", "thread-1"),
        (5, "tenant-b", "app-a", "group-a", "thread-1"),
        (6, "tenant-b", "app-b", "group-b", "thread-1"),
    )
    try:
        for index, tenant, app, group, thread in scopes:
            await store.admit(
                event(index, tenant_key=tenant, app_id=app),
                command(f"{tenant}/{app}/{group}/{thread}", group_id=group),
                now_ms=index,
            )

        leased = await store.lease_tasks(
            worker_id="worker",
            now_ms=10,
            lease_ms=100,
            limit=6,
            limits=limits,
        )

        assert len(leased) == 4
        assert sum(task.tenant_key == "tenant-a" for task in leased) == 2
        assert sum(task.tenant_key == "tenant-b" for task in leased) == 2
        assert len({(task.tenant_key, task.app_id) for task in leased}) == 4
        assert len({(task.tenant_key, task.app_id, task.group_id) for task in leased}) == 4
        assert (
            await store.lease_tasks(
                worker_id="other",
                now_ms=11,
                lease_ms=100,
                limit=6,
                limits=limits,
            )
            == []
        )
    finally:
        await database.close()


async def test_concurrent_lease_requests_cannot_oversubscribe_global_limit(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    limits = TaskConcurrencyLimits(2, 2, 2, 2)
    try:
        for index in range(4):
            await store.admit(
                event(index, tenant_key=f"tenant-{index}"),
                command(f"tenant-{index}/app/group/thread", group_id="group"),
                now_ms=index,
            )

        first, second = await asyncio.gather(
            store.lease_tasks(
                worker_id="worker-a",
                now_ms=10,
                lease_ms=100,
                limit=2,
                limits=limits,
            ),
            store.lease_tasks(
                worker_id="worker-b",
                now_ms=10,
                lease_ms=100,
                limit=2,
                limits=limits,
            ),
        )

        assert len(first) + len(second) == 2
    finally:
        await database.close()


async def test_expired_hierarchical_lease_restores_group_capacity_fairly(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    limits = TaskConcurrencyLimits(1, 1, 1, 1)
    try:
        for index in (1, 2):
            await store.admit(
                event(index),
                command(f"tenant-1/app-1/group/thread-{index}", group_id="group"),
                now_ms=index,
            )
        abandoned = await store.lease_tasks(
            worker_id="crashed",
            now_ms=2,
            lease_ms=10,
            limits=limits,
        )
        assert len(abandoned) == 1
        assert (
            await store.lease_tasks(worker_id="early", now_ms=11, lease_ms=10, limits=limits) == []
        )

        recovered = await store.lease_tasks(
            worker_id="recovered", now_ms=12, lease_ms=10, limits=limits
        )

        assert len(recovered) == 1
        assert recovered[0].session_key != abandoned[0].session_key
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


class DeferredHandler(ConcurrentHandler):
    async def execute(self, task: LeasedTask, *, now_ms: int) -> TaskExecutionResult:
        del task, now_ms
        raise TaskDeferred("approval_pending", delay_ms=25)


class BlockingHandler(ConcurrentHandler):
    def __init__(self) -> None:
        super().__init__(1)
        self.entered = asyncio.Event()

    async def execute(self, task: LeasedTask, *, now_ms: int) -> TaskExecutionResult:
        del task, now_ms
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_task_deferral_releases_lease_without_spending_retry_budget(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    try:
        await store.admit(event(1), command("session-a", max_attempts=1), now_ms=1)
        worker = DurableTaskWorker(
            store,
            {"im.handle_mention": DeferredHandler(1)},
            worker_id="worker",
            clock_ms=lambda: 10,
        )

        batch = await worker.run_once(now_ms=2)

        assert len(batch.retry_task_ids) == 1
        state = await database.call(
            lambda connection: connection.execute(
                "SELECT state, attempt_count, available_at_ms, last_error_code FROM runtime_tasks"
            ).fetchone()
        )
        assert tuple(state) == ("pending", 0, 35, "approval_pending")
    finally:
        await database.close()


async def test_worker_cancellation_releases_lease_without_spending_retry_budget(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    try:
        await store.admit(event(1), command("session-a"), now_ms=1)
        handler = BlockingHandler()
        worker = DurableTaskWorker(
            store,
            {"im.handle_mention": handler},
            worker_id="worker",
            clock_ms=lambda: 10,
        )
        running = asyncio.create_task(worker.run_once(now_ms=2))
        await handler.entered.wait()

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        state = await database.call(
            lambda connection: connection.execute(
                "SELECT state, attempt_count, lease_owner, last_error_code FROM runtime_tasks"
            ).fetchone()
        )
        assert tuple(state) == ("pending", 0, None, "shutdown_cancelled")
    finally:
        await database.close()


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


async def test_scheduler_makes_bounded_progress_across_64_group_sessions(
    tmp_path: Path,
) -> None:
    database, store = await setup(tmp_path)
    graph_store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"t" * 32)))
    supervisor = MultiAgentSupervisor(graph_store)
    try:
        for index in range(64):
            await store.admit(event(index), command(f"group-{index}"), now_ms=index)
        for index in range(64, 68):
            await store.admit(event(index), command("group-0"), now_ms=index)

        completed: list[str] = []
        now_ms = 100
        while len(completed) < 68:
            leased = await store.lease_tasks(
                worker_id="burst-worker",
                now_ms=now_ms,
                lease_ms=100,
                limit=8,
            )
            assert leased
            session_keys = [item.session_key for item in leased]
            assert len(session_keys) == len(set(session_keys))
            for item in leased:
                graph, root = await supervisor.create_graph(
                    root_run_id=item.task_id,
                    tenant_key="tenant-1",
                    app_id="app-1",
                    source_resource_kind="im.thread",
                    source_resource_id=item.session_key,
                    agent_definition_id="burst-root",
                    agent_definition_version=1,
                    root_spec=NodeSpec(
                        "root",
                        AgentRole.ORCHESTRATOR,
                        "Own the isolated group request.",
                        "AgentOutcome",
                        (),
                        {},
                    ),
                    now_ms=now_ms,
                )
                await supervisor.publish_terminal(
                    graph.graph_id,
                    root.node_id,
                    RunStatus.COMPLETED,
                    now_ms=now_ms + 1,
                )
                completed.append(str(item.payload["message_id"]))
                await store.finish_task(
                    item.task_id,
                    worker_id="burst-worker",
                    state=TaskState.SUCCEEDED,
                    now_ms=now_ms + 1,
                )
            now_ms += 2

        assert {f"group-{index}" for index in range(64)} <= set(completed)
        assert completed.count("group-0") == 5
        graphs = await database.call(
            lambda connection: connection.execute(
                "SELECT tenant_key, app_id, source_resource_id, status FROM runtime_graphs"
            ).fetchall()
        )
        assert len(graphs) == 68
        assert all(tuple(row)[:2] == ("tenant-1", "app-1") for row in graphs)
        assert all(tuple(row)[3] == "completed" for row in graphs)
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
