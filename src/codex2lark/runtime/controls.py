from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class RunControlKind(StrEnum):
    STEER = "steer"
    FOLLOW_UP = "follow_up"
    INTERRUPT = "interrupt"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class RunControl:
    control_id: str
    target_task_id: str
    kind: RunControlKind
    text: str
    actor_id: str
    source_message_id: str
    created_at_ms: int


class RunControlInbox(Protocol):
    async def pending_controls(self, task_id: str) -> tuple[RunControl, ...]: ...

    async def acknowledge_controls(
        self, task_id: str, control_ids: tuple[str, ...], *, now_ms: int
    ) -> None: ...
