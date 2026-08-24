from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    event_id: str
    plugin_id: str
    event_type: str
    tenant_key: str
    app_id: str
    occurred_at_ms: int
    received_at_ms: int
    resource_kind: str
    resource_id: str
    trace_id: str
    schema_version: int = 1
    correlation_id: str | None = None
    source_payload: bytes | None = field(default=None, repr=False)
    payload_expires_at_ms: int | None = None

    def __post_init__(self) -> None:
        required = (
            self.event_id,
            self.plugin_id,
            self.event_type,
            self.tenant_key,
            self.app_id,
            self.resource_kind,
            self.resource_id,
            self.trace_id,
        )
        if any(not value.strip() for value in required):
            raise ValueError("event identity fields must be non-empty")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")


@dataclass(frozen=True, slots=True)
class TaskCommand:
    plugin_id: str
    command_type: str
    session_key: str
    payload: dict[str, Any]
    priority: int = 0
    available_at_ms: int = 0
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not self.plugin_id or not self.command_type or not self.session_key:
            raise ValueError("task routing fields must be non-empty")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


@dataclass(frozen=True, slots=True)
class OutboxDraft:
    publisher_id: str
    destination_ref: str
    message_kind: str
    idempotency_key: str
    payload: dict[str, Any]
    available_at_ms: int = 0

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (
                self.publisher_id,
                self.destination_ref,
                self.message_kind,
                self.idempotency_key,
            )
        ):
            raise ValueError("outbox routing fields must be non-empty")


@dataclass(frozen=True, slots=True)
class LeasedTask:
    task_id: str
    event_id: str | None
    plugin_id: str
    command_type: str
    session_key: str
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int
    lease_expires_at_ms: int
    recovery_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LeasedOutboxMessage:
    outbox_id: str
    run_id: str | None
    publisher_id: str
    destination_ref: str
    message_kind: str
    idempotency_key: str
    payload: dict[str, Any]
    attempt_count: int
    lease_expires_at_ms: int
