from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from .types import AgentOutcome, RunCheckpoint, RunEvent, RunStatus


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

    async def run_status(self, run_id: str) -> RunStatus | None: ...

    async def load_outcome(self, run_id: str) -> AgentOutcome | None: ...

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

    async def run_status(self, run_id: str) -> RunStatus | None:
        return self.runs.get(run_id)

    async def load_outcome(self, run_id: str) -> AgentOutcome | None:
        terminal = next(
            (
                event
                for event in reversed(self.events.get(run_id, []))
                if event.event_type == "run_terminal"
            ),
            None,
        )
        if terminal is None:
            return None
        return AgentOutcome(
            status=RunStatus(str(terminal.payload["status"])),
            summary=str(terminal.payload["summary"]),
            resource_refs=tuple(str(item) for item in terminal.payload["resource_refs"]),
            warnings=tuple(str(item) for item in terminal.payload["warnings"]),
        )

    async def finish_run(self, run_id: str, status: RunStatus, *, now_ms: int) -> None:
        del now_ms
        if status in (RunStatus.RUNNING, RunStatus.WAITING):
            raise ValueError("finish_run requires a terminal status")
        self.runs[run_id] = status
