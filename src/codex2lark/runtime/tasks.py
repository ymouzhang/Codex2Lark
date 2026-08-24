from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from codex2lark.core.events import LeasedTask, OutboxDraft, TaskState


class PermanentTaskError(RuntimeError):
    pass


class TaskDeferred(RuntimeError):
    def __init__(self, reason: str, *, delay_ms: int = 2_000) -> None:
        if not reason or delay_ms < 0:
            raise ValueError("task deferral is invalid")
        super().__init__(reason)
        self.reason = reason
        self.delay_ms = delay_ms


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    state: TaskState
    terminal_message: OutboxDraft | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {
            TaskState.SUCCEEDED,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }:
            raise ValueError("task execution result must be terminal")


class TaskHandler(Protocol):
    async def execute(self, task: LeasedTask, *, now_ms: int) -> TaskExecutionResult: ...

    def failure(self, task: LeasedTask, error: BaseException) -> TaskExecutionResult: ...


class TaskStore(Protocol):
    async def lease_tasks(
        self, *, worker_id: str, now_ms: int, lease_ms: int, limit: int = 1
    ) -> list[LeasedTask]: ...

    async def finish_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        state: TaskState,
        now_ms: int,
        error_code: str | None = None,
        terminal_message: OutboxDraft | None = None,
    ) -> None: ...

    async def retry_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        available_at_ms: int,
        now_ms: int,
        error_code: str,
    ) -> None: ...

    async def defer_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        available_at_ms: int,
        now_ms: int,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TaskBatch:
    terminal_task_ids: tuple[str, ...]
    retry_task_ids: tuple[str, ...]


class DurableTaskWorker:
    def __init__(
        self,
        store: TaskStore,
        handlers: dict[str, TaskHandler],
        *,
        worker_id: str,
        lease_ms: int = 300_000,
        retry_delay_ms: int = 2_000,
        concurrency: int = 4,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not worker_id or min(lease_ms, concurrency) < 1 or retry_delay_ms < 0:
            raise ValueError("task worker configuration is invalid")
        self._store = store
        self._handlers = dict(handlers)
        self._worker_id = worker_id
        self._lease_ms = lease_ms
        self._retry_delay_ms = retry_delay_ms
        self._concurrency = concurrency
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    async def run_once(self, *, now_ms: int) -> TaskBatch:
        tasks = await self._store.lease_tasks(
            worker_id=self._worker_id,
            now_ms=now_ms,
            lease_ms=self._lease_ms,
            limit=self._concurrency,
        )
        results = await asyncio.gather(*(self._execute(task, now_ms=now_ms) for task in tasks))
        return TaskBatch(
            terminal_task_ids=tuple(task_id for task_id, retrying in results if not retrying),
            retry_task_ids=tuple(task_id for task_id, retrying in results if retrying),
        )

    async def _execute(self, task: LeasedTask, *, now_ms: int) -> tuple[str, bool]:
        handler = self._handlers.get(task.command_type)
        try:
            if handler is None:
                raise RuntimeError(f"task handler is unavailable: {task.command_type}")
            if task.recovery_error_code is not None:
                raise PermanentTaskError(task.recovery_error_code)
            result = await handler.execute(task, now_ms=now_ms)
        except asyncio.CancelledError:
            transition_ms = max(now_ms, self._clock_ms())
            await self._store.defer_task(
                task.task_id,
                worker_id=self._worker_id,
                available_at_ms=transition_ms,
                now_ms=transition_ms,
                reason="shutdown_cancelled",
            )
            raise
        except TaskDeferred as exc:
            transition_ms = max(now_ms, self._clock_ms())
            await self._store.defer_task(
                task.task_id,
                worker_id=self._worker_id,
                available_at_ms=transition_ms + exc.delay_ms,
                now_ms=transition_ms,
                reason=exc.reason,
            )
            return task.task_id, True
        except Exception as exc:
            transition_ms = max(now_ms, self._clock_ms())
            if not isinstance(exc, PermanentTaskError) and task.attempt_count < task.max_attempts:
                await self._store.retry_task(
                    task.task_id,
                    worker_id=self._worker_id,
                    available_at_ms=transition_ms + self._retry_delay_ms,
                    now_ms=transition_ms,
                    error_code=type(exc).__name__,
                )
                return task.task_id, True
            result = (
                handler.failure(task, exc)
                if handler is not None
                else TaskExecutionResult(
                    TaskState.FAILED,
                    error_code="handler_unavailable",
                )
            )
        transition_ms = max(now_ms, self._clock_ms())
        await self._store.finish_task(
            task.task_id,
            worker_id=self._worker_id,
            state=result.state,
            now_ms=transition_ms,
            error_code=result.error_code,
            terminal_message=result.terminal_message,
        )
        return task.task_id, False
