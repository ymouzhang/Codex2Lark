from __future__ import annotations

from typing import NewType
from uuid import uuid4

EventId = NewType("EventId", str)
TaskId = NewType("TaskId", str)
RunId = NewType("RunId", str)
OutboxId = NewType("OutboxId", str)
TraceId = NewType("TraceId", str)


def new_event_id() -> EventId:
    return EventId(str(uuid4()))


def new_task_id() -> TaskId:
    return TaskId(str(uuid4()))


def new_run_id() -> RunId:
    return RunId(str(uuid4()))


def new_outbox_id() -> OutboxId:
    return OutboxId(str(uuid4()))


def new_trace_id() -> TraceId:
    return TraceId(str(uuid4()))
