from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .types import (
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    VerificationRecord,
    VerificationState,
)


@dataclass(frozen=True, slots=True)
class ToolContext:
    run_id: str
    node_id: str
    tenant_key: str
    app_id: str
    actor_id: str
    session_key: str
    identity_ref: str
    policy_version: int
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str
    approval_required: bool = False


class ToolPolicy(Protocol):
    async def authorize(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: ToolContext,
    ) -> PolicyDecision: ...


class ApprovalBroker(Protocol):
    async def request(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: ToolContext,
    ) -> bool: ...


class SemanticTool(Protocol):
    definition: ToolDefinition

    def validate(self, arguments: dict[str, object]) -> None: ...

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolContext,
    ) -> dict[str, object]: ...

    async def verify(
        self,
        arguments: dict[str, object],
        observation: dict[str, object],
        context: ToolContext,
    ) -> VerificationRecord: ...


class ToolRegistry:
    def __init__(self, tools: list[SemanticTool]) -> None:
        self._tools: dict[str, SemanticTool] = {}
        for tool in tools:
            tool_id = tool.definition.tool_id
            if tool_id in self._tools:
                raise ValueError(f"duplicate semantic tool: {tool_id}")
            self._tools[tool_id] = tool

    def definitions(self, allowed_ids: tuple[str, ...]) -> tuple[ToolDefinition, ...]:
        return tuple(self.require(tool_id).definition for tool_id in allowed_ids)

    def require(self, tool_id: str) -> SemanticTool:
        tool = self._tools.get(tool_id)
        if tool is None:
            raise LookupError(f"semantic tool is unavailable: {tool_id}")
        return tool


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolPolicy,
        approvals: ApprovalBroker,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._approvals = approvals

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        try:
            tool = self._registry.require(call.tool_id)
        except LookupError as exc:
            return self._failure(call, ToolEffect.READ, "tool_unavailable", str(exc))
        definition = tool.definition
        try:
            tool.validate(call.arguments)
        except (TypeError, ValueError) as exc:
            return self._failure(call, definition.effect, "validation_error", str(exc))
        decision = await self._policy.authorize(definition, call, context)
        if not decision.allowed:
            return self._failure(call, definition.effect, "policy_error", decision.reason)
        if decision.approval_required and not await self._approvals.request(
            definition, call, context
        ):
            return self._failure(call, definition.effect, "approval_denied", "approval denied")
        try:
            observation = await tool.execute(call.arguments, context)
            verification = await tool.verify(call.arguments, observation, context)
        except TimeoutError as exc:
            return self._failure(call, definition.effect, "timeout_error", str(exc))
        except Exception as exc:
            return self._failure(call, definition.effect, "tool_error", str(exc))
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            observation=observation,
            effect=definition.effect,
            verification=verification,
            checkpoint_safe_observation=getattr(tool, "checkpoint_safe_observation", True),
        )

    @staticmethod
    def _failure(call: ToolCall, effect: ToolEffect, code: str, summary: str) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            observation={},
            effect=effect,
            verification=VerificationRecord(
                state=VerificationState.FAILED,
                verifier_id="harness",
                summary=summary,
            ),
            error_code=code,
        )
