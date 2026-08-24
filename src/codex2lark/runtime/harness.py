from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

from codex2lark.core.budgets import BudgetKind, BudgetLedger
from codex2lark.core.cancellation import CancellationToken, CancelledByPolicyError

from .context import ContextEngine, ContextEvidence
from .resources import ResourceLoader
from .sessions import SessionStore
from .tools import ToolContext, ToolExecutor, ToolRegistry
from .types import (
    AgentDefinition,
    AgentOutcome,
    MessageRole,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RunCheckpoint,
    RunStatus,
    ToolEffect,
    VerificationRecord,
    VerificationState,
)


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class HarnessRequest:
    run_id: str
    task_id: str
    node_id: str
    user_request: str
    tool_context: ToolContext
    evidence: tuple[ContextEvidence, ...] = ()


class AgentHarness:
    def __init__(
        self,
        *,
        model: ModelProvider,
        tools: ToolRegistry,
        tool_executor: ToolExecutor,
        resources: ResourceLoader,
        context: ContextEngine,
        sessions: SessionStore,
    ) -> None:
        self._model = model
        self._tools = tools
        self._tool_executor = tool_executor
        self._resources = resources
        self._context = context
        self._sessions = sessions

    async def run(
        self,
        request: HarnessRequest,
        definition: AgentDefinition,
        *,
        cancellation: CancellationToken | None = None,
        resume: bool = False,
        now_ms: int | None = None,
    ) -> AgentOutcome:
        token = cancellation or CancellationToken()
        clock_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        loaded = self._resources.load(definition.resource_packages)
        ledger = BudgetLedger.from_limits(list(definition.budget_limits))
        journal: tuple[ModelMessage, ...] = ()
        verified: tuple[VerificationRecord, ...] = ()
        source_versions = {item.source_ref: item.source_version for item in request.evidence}
        first_turn = 1

        if resume:
            checkpoint = await self._sessions.load_checkpoint(request.run_id)
            if checkpoint is None:
                raise LookupError(f"run checkpoint is unavailable: {request.run_id}")
            self._validate_checkpoint(checkpoint, definition, loaded.versions, source_versions)
            journal = checkpoint.messages
            verified = checkpoint.verified_effects
            first_turn = checkpoint.next_turn
            for key, amount in checkpoint.consumed_budget.items():
                kind = BudgetKind(key)
                if kind in ledger.limits:
                    ledger.consumed[kind] = amount
        else:
            await self._sessions.start_run(
                run_id=request.run_id,
                task_id=request.task_id,
                session_key=request.tool_context.session_key,
                agent_id=definition.agent_id,
                agent_version=definition.version,
                policy_version=definition.policy_version,
                now_ms=clock_ms,
            )
            await self._event(
                request.run_id,
                "run_started",
                {
                    "agent_id": definition.agent_id,
                    "agent_version": definition.version,
                    "node_id": request.node_id,
                },
                clock_ms,
            )

        try:
            for turn in range(first_turn, definition.max_turns + 1):
                token.raise_if_cancelled()
                context = self._context.build(
                    definition=definition,
                    resources=loaded,
                    user_request=request.user_request,
                    evidence=request.evidence,
                    journal=journal,
                )
                model_request = ModelRequest(
                    run_id=request.run_id,
                    node_id=request.node_id,
                    model_profile=definition.model_profile,
                    messages=context.messages,
                    tools=self._tools.definitions(definition.tool_ids),
                    remaining_budget={kind.value: ledger.available(kind) for kind in ledger.limits},
                )
                await self._event(
                    request.run_id,
                    "turn_started",
                    {
                        "turn": turn,
                        "context_tokens": context.estimated_tokens,
                        "truncated_sources": list(context.truncated_sources),
                    },
                    clock_ms,
                )
                response = await self._model.complete(model_request)
                self._consume_if_limited(
                    ledger,
                    BudgetKind.MODEL_TOKENS,
                    response.usage.input_tokens + response.usage.output_tokens,
                )
                self._consume_if_limited(ledger, BudgetKind.COST_MICROS, response.usage.cost_micros)
                assistant = ModelMessage(
                    MessageRole.ASSISTANT,
                    response.content,
                    tool_calls=response.tool_calls,
                )
                journal = (*journal, assistant)
                await self._event(
                    request.run_id,
                    "model_completed",
                    {
                        "turn": turn,
                        "tool_call_count": len(response.tool_calls),
                        "provider_response_id": response.provider_response_id,
                        "usage": {
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                            "cost_micros": response.usage.cost_micros,
                        },
                    },
                    clock_ms,
                )

                if not response.tool_calls:
                    outcome = self._outcome(response.content, definition, verified)
                    return await self._finish(request.run_id, outcome, clock_ms)

                for call in response.tool_calls:
                    token.raise_if_cancelled()
                    self._consume_if_limited(ledger, BudgetKind.TOOL_CALLS, 1)
                    await self._event(
                        request.run_id,
                        "tool_requested",
                        {"call_id": call.call_id, "tool_id": call.tool_id},
                        clock_ms,
                    )
                    result = await self._tool_executor.execute(call, request.tool_context)
                    if result.effect in (ToolEffect.WRITE, ToolEffect.DESTRUCTIVE):
                        self._consume_if_limited(ledger, BudgetKind.EXTERNAL_WRITES, 1)
                    if result.verification.state is VerificationState.VERIFIED:
                        verified = (*verified, result.verification)
                    journal = (
                        *journal,
                        ModelMessage(
                            MessageRole.TOOL,
                            json.dumps(
                                {
                                    "observation": result.observation,
                                    "error_code": result.error_code,
                                    "verification": {
                                        "state": result.verification.state.value,
                                        "summary": result.verification.summary,
                                        "resource_refs": result.verification.resource_refs,
                                    },
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            name=result.tool_id,
                            tool_call_id=result.call_id,
                        ),
                    )
                    await self._event(
                        request.run_id,
                        "tool_completed" if result.succeeded else "tool_failed",
                        {
                            "call_id": result.call_id,
                            "tool_id": result.tool_id,
                            "error_code": result.error_code,
                            "verification": result.verification.state.value,
                        },
                        clock_ms,
                    )

                checkpoint = RunCheckpoint(
                    run_id=request.run_id,
                    agent_id=definition.agent_id,
                    agent_version=definition.version,
                    resource_versions=loaded.versions,
                    next_turn=turn + 1,
                    messages=journal,
                    verified_effects=verified,
                    blockers=(),
                    source_versions=context.source_versions,
                    consumed_budget={
                        kind.value: amount for kind, amount in ledger.consumed.items()
                    },
                    compactor_version=definition.compactor_version,
                )
                await self._sessions.save_checkpoint(checkpoint, now_ms=clock_ms)
                await self._event(
                    request.run_id,
                    "checkpoint_saved",
                    {"next_turn": checkpoint.next_turn},
                    clock_ms,
                )

            outcome = AgentOutcome(
                status=RunStatus.FAILED,
                summary="The Agent reached its maximum turn budget before completing the task.",
                warnings=("turn_budget_exhausted",),
            )
            return await self._finish(request.run_id, outcome, clock_ms)
        except CancelledByPolicyError as exc:
            outcome = AgentOutcome(status=RunStatus.CANCELLED, summary=str(exc))
            return await self._finish(request.run_id, outcome, clock_ms)
        except (LookupError, ValueError, RuntimeError) as exc:
            outcome = AgentOutcome(
                status=RunStatus.FAILED,
                summary=str(exc),
                warnings=(type(exc).__name__,),
            )
            return await self._finish(request.run_id, outcome, clock_ms)

    async def _finish(self, run_id: str, outcome: AgentOutcome, now_ms: int) -> AgentOutcome:
        await self._event(
            run_id,
            "run_terminal",
            {
                "status": outcome.status.value,
                "summary": outcome.summary,
                "resource_refs": list(outcome.resource_refs),
                "warnings": list(outcome.warnings),
            },
            now_ms,
        )
        await self._sessions.finish_run(run_id, outcome.status, now_ms=now_ms)
        return outcome

    async def _event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        now_ms: int,
    ) -> None:
        await self._sessions.append_event(
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            now_ms=now_ms,
        )

    @staticmethod
    def _consume_if_limited(ledger: BudgetLedger, kind: BudgetKind, amount: int) -> None:
        if kind in ledger.limits:
            ledger.consume(kind, amount)

    @staticmethod
    def _outcome(
        content: str,
        definition: AgentDefinition,
        verified: tuple[VerificationRecord, ...],
    ) -> AgentOutcome:
        verified_refs = tuple(
            reference for record in verified for reference in record.resource_refs
        )
        if definition.require_verified_external_effect and not verified:
            return AgentOutcome(
                status=RunStatus.FAILED,
                summary=(
                    "The Agent produced a response but no required external effect was verified."
                ),
                warnings=("verification_missing",),
            )
        return AgentOutcome(
            status=RunStatus.COMPLETED,
            summary=content or "Completed.",
            resource_refs=verified_refs,
        )

    @staticmethod
    def _validate_checkpoint(
        checkpoint: RunCheckpoint,
        definition: AgentDefinition,
        resource_versions: dict[str, str],
        source_versions: dict[str, str],
    ) -> None:
        if (checkpoint.agent_id, checkpoint.agent_version) != (
            definition.agent_id,
            definition.version,
        ):
            raise ValueError("checkpoint AgentDefinition is incompatible")
        if checkpoint.resource_versions != resource_versions:
            raise ValueError("checkpoint resources are incompatible")
        if checkpoint.source_versions != source_versions:
            raise ValueError("checkpoint source evidence is stale")
        if checkpoint.compactor_version != definition.compactor_version:
            raise ValueError("checkpoint compactor is incompatible")
