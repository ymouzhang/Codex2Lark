from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from codex2lark.core.budgets import BudgetKind, BudgetLimit


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class VerificationState(StrEnum):
    NOT_REQUIRED = "not_required"
    VERIFIED = "verified"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    trusted: bool = False
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    tool_id: str
    version: int
    description: str
    input_schema: dict[str, Any]
    effect: ToolEffect


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool_id: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)
    provider_response_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRequest:
    run_id: str
    node_id: str
    model_profile: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    remaining_budget: dict[str, int]


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    state: VerificationState
    verifier_id: str
    summary: str
    resource_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_id: str
    observation: dict[str, Any]
    effect: ToolEffect
    verification: VerificationRecord
    error_code: str | None = None
    checkpoint_safe_observation: bool = True

    @property
    def succeeded(self) -> bool:
        return self.error_code is None


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    version: int
    instructions: str
    model_profile: str
    tool_ids: tuple[str, ...]
    resource_packages: tuple[str, ...] = ()
    budget_limits: tuple[BudgetLimit, ...] = ()
    max_turns: int = 12
    max_context_tokens: int = 64_000
    require_verified_external_effect: bool = False
    policy_version: int = 1
    compactor_version: int = 1

    def __post_init__(self) -> None:
        if not self.agent_id or self.version < 1 or not self.instructions:
            raise ValueError("AgentDefinition identity and instructions are required")
        if self.max_turns < 1 or self.max_context_tokens < 1:
            raise ValueError("AgentDefinition limits must be positive")
        if len(set(self.tool_ids)) != len(self.tool_ids):
            raise ValueError("AgentDefinition tool_ids must be unique")

    def budget_limit(self, kind: BudgetKind) -> int:
        return next((item.maximum for item in self.budget_limits if item.kind is kind), 0)


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    run_id: str
    agent_id: str
    agent_version: int
    resource_versions: dict[str, str]
    next_turn: int
    messages: tuple[ModelMessage, ...]
    verified_effects: tuple[VerificationRecord, ...]
    blockers: tuple[str, ...]
    source_versions: dict[str, str]
    consumed_budget: dict[str, int]
    compactor_version: int


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    status: RunStatus
    summary: str
    resource_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status is RunStatus.RUNNING:
            raise ValueError("AgentOutcome must be terminal or waiting")
