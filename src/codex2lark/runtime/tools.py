from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .capacity import CapacityLane, FairCapacityGate
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
    chat_id: str | None = None
    source_message_id: str | None = None
    reply_in_thread: bool = False
    write_scope: tuple[WriteScopeTarget, ...] = ()
    write_scope_required: bool = False


@dataclass(frozen=True, slots=True)
class WriteScopeTarget:
    resource_type: str
    resource_id: str
    expected_revision: str | None = None

    def __post_init__(self) -> None:
        if not self.resource_type or not self.resource_id:
            raise ValueError("write scope target identity is required")


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

    async def reconcile(
        self,
        arguments: dict[str, object],
        context: ToolContext,
    ) -> ToolReconciliation: ...


@dataclass(frozen=True, slots=True)
class ToolReconciliation:
    observation: dict[str, object]
    verification: VerificationRecord
    safe_to_execute: bool = False


class ToolOperationStore(Protocol):
    async def claim_idempotency(
        self,
        *,
        key: str,
        operation_kind: str,
        owner: str,
        expires_at_ms: int,
        now_ms: int,
    ) -> object: ...

    async def complete_idempotency(
        self, *, key: str, owner: str, result_ref: str, now_ms: int
    ) -> None: ...


class WriteScopeLeaseStore(Protocol):
    async def owns_write_scope(
        self,
        owner_id: str,
        targets: tuple[WriteScopeTarget, ...],
        *,
        now_ms: int,
    ) -> bool: ...

    async def renew_write_scope(
        self,
        owner_id: str,
        targets: tuple[WriteScopeTarget, ...],
        *,
        now_ms: int,
        lease_ms: int,
    ) -> bool: ...


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

    def batch_parallel_safe(self, calls: tuple[ToolCall, ...]) -> bool:
        if len(calls) < 2:
            return False
        try:
            tools = tuple(self.require(call.tool_id) for call in calls)
        except LookupError:
            return False
        for tool, call in zip(tools, calls, strict=True):
            definition = tool.definition
            if definition.effect is not ToolEffect.READ or not definition.parallel_safe:
                return False
            guard = getattr(tool, "parallel_safe_for", None)
            if callable(guard) and not bool(guard(call.arguments)):
                return False
        return True

    def schema_fingerprint(self, allowed_ids: tuple[str, ...]) -> str:
        canonical = [
            {
                "tool_id": definition.tool_id,
                "version": definition.version,
                "effect": definition.effect.value,
                "parallel_safe": definition.parallel_safe,
                "input_schema": definition.input_schema,
            }
            for definition in self.definitions(allowed_ids)
        ]
        encoded = json.dumps(
            canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolPolicy,
        approvals: ApprovalBroker,
        operation_store: ToolOperationStore | None = None,
        write_scope_store: WriteScopeLeaseStore | None = None,
        clock_ms: Callable[[], int] | None = None,
        claim_lease_ms: int = 300_000,
        write_lock_lease_ms: int = 300_000,
        capacity_gate: FairCapacityGate | None = None,
        tool_plugin_ids: dict[str, str] | None = None,
        plugin_concurrency: int | None = None,
    ) -> None:
        if min(claim_lease_ms, write_lock_lease_ms) < 1:
            raise ValueError("tool operation claim lease must be positive")
        if capacity_gate is None and tool_plugin_ids:
            raise ValueError("tool plugin ownership requires a capacity gate")
        if capacity_gate is not None and (plugin_concurrency is None or plugin_concurrency < 1):
            raise ValueError("plugin concurrency must be positive with a capacity gate")
        self._registry = registry
        self._policy = policy
        self._approvals = approvals
        self._operation_store = operation_store
        self._write_scope_store = write_scope_store
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._claim_lease_ms = claim_lease_ms
        self._write_lock_lease_ms = write_lock_lease_ms
        self._capacity_gate = capacity_gate
        self._tool_plugin_ids = dict(tool_plugin_ids or {})
        self._plugin_concurrency = plugin_concurrency

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
            return self._failure(
                call,
                definition.effect,
                "approval_rejected",
                "the requester rejected or did not approve this operation",
            )
        return await self._execute_with_capacity(tool, definition, call, context)

    async def _execute_with_capacity(
        self,
        tool: SemanticTool,
        definition: ToolDefinition,
        call: ToolCall,
        context: ToolContext,
    ) -> ToolResult:
        plugin_id = self._tool_plugin_ids.get(definition.tool_id)
        if plugin_id is None or self._capacity_gate is None:
            return await self._execute_permitted(tool, definition, call, context)
        assert self._plugin_concurrency is not None
        lane = CapacityLane(
            context.tenant_key,
            context.app_id,
            context.chat_id or context.session_key,
        )
        async with self._capacity_gate.capacity(
            f"plugin:{plugin_id}", lane, limit=self._plugin_concurrency
        ):
            return await self._execute_permitted(tool, definition, call, context)

    async def _execute_permitted(
        self,
        tool: SemanticTool,
        definition: ToolDefinition,
        call: ToolCall,
        context: ToolContext,
    ) -> ToolResult:
        if (
            context.write_scope_required
            and not context.write_scope
            and definition.effect in (ToolEffect.WRITE, ToolEffect.DESTRUCTIVE)
        ):
            return self._failure(
                call,
                definition.effect,
                "write_lock_missing",
                "the delegated writer no longer owns its required resource lock",
            )
        if (
            context.write_scope_required
            and self._write_scope_store is None
            and definition.effect in (ToolEffect.WRITE, ToolEffect.DESTRUCTIVE)
        ):
            return self._failure(
                call,
                definition.effect,
                "write_lock_validator_missing",
                "the delegated writer has no runtime lock validator",
            )
        if context.write_scope and definition.effect in (ToolEffect.WRITE, ToolEffect.DESTRUCTIVE):
            resolver = getattr(tool, "resolve_write_target", None)
            if not callable(resolver):
                return self._failure(
                    call,
                    definition.effect,
                    "write_scope_unsupported",
                    "the scoped writer tool cannot resolve its live target",
                )
            try:
                actual = await resolver(call.arguments, context)
            except Exception as exc:
                return self._failure(
                    call,
                    definition.effect,
                    "write_scope_resolution_failed",
                    f"live write target resolution failed: {type(exc).__name__}",
                )
            allowed = next(
                (
                    target
                    for target in context.write_scope
                    if (target.resource_type, target.resource_id)
                    == (actual.resource_type, actual.resource_id)
                ),
                None,
            )
            if allowed is None:
                return self._failure(
                    call,
                    definition.effect,
                    "write_scope_violation",
                    "the actual write target is outside the delegated lock scope",
                )
            if (
                allowed.expected_revision is not None
                and actual.expected_revision != allowed.expected_revision
            ):
                return self._failure(
                    call,
                    definition.effect,
                    "write_revision_conflict",
                    "the live target revision changed after the delegated lock was acquired",
                )
            if (
                context.write_scope_required
                and self._write_scope_store is not None
                and not await self._write_scope_store.renew_write_scope(
                    context.run_id,
                    context.write_scope,
                    now_ms=self._clock_ms(),
                    lease_ms=self._write_lock_lease_ms,
                )
            ):
                return self._failure(
                    call,
                    definition.effect,
                    "write_lock_expired",
                    "the delegated writer's durable resource lock expired",
                )
        operation_key: str | None = None
        owner = f"{context.run_id}:{context.node_id}"
        if definition.effect in (ToolEffect.WRITE, ToolEffect.DESTRUCTIVE):
            operation_key = self._operation_key(call, definition, context)
            replay = await self._claim_write(operation_key, call, definition, tool, context, owner)
            if replay is not None:
                return replay
        try:
            observation = await tool.execute(call.arguments, context)
            verification = await tool.verify(call.arguments, observation, context)
        except TimeoutError as exc:
            return self._failure(call, definition.effect, "timeout_error", str(exc))
        except Exception as exc:
            return self._failure(call, definition.effect, "tool_error", str(exc))
        if (
            operation_key is not None
            and self._operation_store is not None
            and verification.state is VerificationState.VERIFIED
            and verification.resource_refs
        ):
            await self._operation_store.complete_idempotency(
                key=operation_key,
                owner=owner,
                result_ref=verification.resource_refs[0],
                now_ms=self._clock_ms(),
            )
        return ToolResult(
            call_id=call.call_id,
            tool_id=call.tool_id,
            observation=observation,
            effect=definition.effect,
            verification=verification,
            checkpoint_safe_observation=getattr(tool, "checkpoint_safe_observation", True),
        )

    async def _claim_write(
        self,
        key: str,
        call: ToolCall,
        definition: ToolDefinition,
        tool: SemanticTool,
        context: ToolContext,
        owner: str,
    ) -> ToolResult | None:
        if self._operation_store is None:
            return None
        now_ms = self._clock_ms()
        claim = await self._operation_store.claim_idempotency(
            key=key,
            operation_kind=definition.tool_id,
            owner=owner,
            expires_at_ms=now_ms + self._claim_lease_ms,
            now_ms=now_ms,
        )
        state = str(getattr(claim, "state", "invalid"))
        result_ref = getattr(claim, "result_ref", None)
        if state == "completed" and isinstance(result_ref, str) and result_ref:
            return ToolResult(
                call.call_id,
                call.tool_id,
                {"resource": {"reference": result_ref}, "idempotent_replay": True},
                definition.effect,
                VerificationRecord(
                    VerificationState.VERIFIED,
                    "operation_journal",
                    "verified completed operation reused",
                    (result_ref,),
                ),
                checkpoint_safe_observation=False,
            )
        if not bool(getattr(claim, "acquired", False)):
            return self._uncertain(
                call, definition.effect, "operation_in_progress", "write claim is still active"
            )
        if not bool(getattr(claim, "recovery_required", False)):
            return None
        try:
            reconciliation = await tool.reconcile(call.arguments, context)
        except Exception as exc:
            return self._uncertain(
                call,
                definition.effect,
                "ambiguous_external_effect",
                f"live reconciliation failed: {type(exc).__name__}",
            )
        if reconciliation.verification.state is VerificationState.VERIFIED:
            refs = reconciliation.verification.resource_refs
            if refs:
                await self._operation_store.complete_idempotency(
                    key=key,
                    owner=owner,
                    result_ref=refs[0],
                    now_ms=self._clock_ms(),
                )
            return ToolResult(
                call.call_id,
                call.tool_id,
                reconciliation.observation,
                definition.effect,
                reconciliation.verification,
                checkpoint_safe_observation=getattr(tool, "checkpoint_safe_observation", True),
            )
        if reconciliation.safe_to_execute:
            return None
        return ToolResult(
            call.call_id,
            call.tool_id,
            reconciliation.observation,
            definition.effect,
            reconciliation.verification,
            error_code="ambiguous_external_effect",
            checkpoint_safe_observation=getattr(tool, "checkpoint_safe_observation", True),
        )

    @staticmethod
    def _operation_key(call: ToolCall, definition: ToolDefinition, context: ToolContext) -> str:
        canonical = json.dumps(call.arguments, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        material = (
            f"tool-operation:v1:{context.run_id}:{context.node_id}:"
            f"{definition.tool_id}:{definition.version}:{digest}"
        )
        return hashlib.sha256(material.encode()).hexdigest()

    @staticmethod
    def _uncertain(call: ToolCall, effect: ToolEffect, code: str, summary: str) -> ToolResult:
        return ToolResult(
            call.call_id,
            call.tool_id,
            {},
            effect,
            VerificationRecord(VerificationState.UNCERTAIN, "operation_journal", summary),
            error_code=code,
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
