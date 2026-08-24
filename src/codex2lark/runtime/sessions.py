from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from .types import RunCheckpoint, RunEvent, RunStatus


class SessionStore(Protocol):
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
    ) -> None: ...

    async def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        now_ms: int,
    ) -> RunEvent: ...

    async def save_checkpoint(self, checkpoint: RunCheckpoint, *, now_ms: int) -> None: ...

    async def load_checkpoint(self, run_id: str) -> RunCheckpoint | None: ...

    async def finish_run(self, run_id: str, status: RunStatus, *, now_ms: int) -> None: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self.runs: dict[str, RunStatus] = {}
        self.events: dict[str, list[RunEvent]] = defaultdict(list)
        self.checkpoints: dict[str, RunCheckpoint] = {}

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
        del task_id, session_key, agent_id, agent_version, policy_version, now_ms
        if run_id in self.runs:
            raise ValueError(f"run already exists: {run_id}")
        self.runs[run_id] = RunStatus.RUNNING

    async def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        now_ms: int,
    ) -> RunEvent:
        if run_id not in self.runs:
            raise LookupError(f"run does not exist: {run_id}")
        event = RunEvent(
            run_id=run_id,
            sequence=len(self.events[run_id]) + 1,
            event_type=event_type,
            payload=payload,
            created_at_ms=now_ms,
        )
        self.events[run_id].append(event)
        return event

    async def save_checkpoint(self, checkpoint: RunCheckpoint, *, now_ms: int) -> None:
        del now_ms
        self.checkpoints[checkpoint.run_id] = checkpoint

    async def load_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        return self.checkpoints.get(run_id)

    async def finish_run(self, run_id: str, status: RunStatus, *, now_ms: int) -> None:
        del now_ms
        if status in (RunStatus.RUNNING, RunStatus.WAITING):
            raise ValueError("finish_run requires a terminal status")
        self.runs[run_id] = status
