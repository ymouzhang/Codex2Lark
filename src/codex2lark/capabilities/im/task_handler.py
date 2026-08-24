from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from codex2lark.core.events import LeasedTask, OutboxDraft, TaskState
from codex2lark.runtime.context import ContextEvidence
from codex2lark.runtime.harness import AgentHarness, HarnessRequest
from codex2lark.runtime.sessions import SessionStore
from codex2lark.runtime.tasks import TaskExecutionResult
from codex2lark.runtime.tools import ToolContext
from codex2lark.runtime.types import AgentDefinition, AgentOutcome, RunStatus

from .context_provider import IMContextProvider, IMContextRequest


class AgentGraphLifecycle(Protocol):
    async def prepare(
        self,
        *,
        run_id: str,
        task: LeasedTask,
        binding: dict[str, str],
        definition: AgentDefinition,
        now_ms: int,
    ) -> None: ...

    async def finish(self, run_id: str, status: RunStatus, *, now_ms: int) -> None: ...


class TaskOutbox(Protocol):
    async def enqueue_task_outbox(
        self, task_id: str, draft: OutboxDraft, *, now_ms: int
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class IMResponseTemplates:
    progress_started: str
    completed_suffix: str
    blocked_suffix: str
    failed_suffix: str
    cancelled_suffix: str

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.completed_suffix,
                self.progress_started,
                self.blocked_suffix,
                self.failed_suffix,
                self.cancelled_suffix,
            )
        ):
            raise ValueError("all IM terminal response templates are required")


class IMMentionTaskHandler:
    def __init__(
        self,
        *,
        context: IMContextProvider,
        harness: AgentHarness,
        sessions: SessionStore,
        definition: AgentDefinition,
        templates: IMResponseTemplates,
        identity_ref: str,
        task_outbox: TaskOutbox,
        graph_lifecycle: AgentGraphLifecycle | None = None,
    ) -> None:
        if not identity_ref:
            raise ValueError("IM execution identity reference is required")
        self._context = context
        self._harness = harness
        self._sessions = sessions
        self._definition = definition
        self._templates = templates
        self._identity_ref = identity_ref
        self._task_outbox = task_outbox
        self._graph_lifecycle = graph_lifecycle

    async def execute(self, task: LeasedTask, *, now_ms: int) -> TaskExecutionResult:
        binding = self._binding(task)
        run_id = self.run_id_for_task(task.task_id)
        await self._task_outbox.enqueue_task_outbox(
            task.task_id,
            OutboxDraft(
                publisher_id="feishu-im.reply",
                destination_ref=binding["message_id"],
                message_kind="progress",
                idempotency_key=(
                    f"im:{binding['tenant_key']}:{binding['app_id']}:"
                    f"{binding['message_id']}:progress:started:v1"
                ),
                payload={
                    "chat_id": binding["chat_id"],
                    "message_id": binding["message_id"],
                    "reply_in_thread": bool(task.payload.get("thread_id")),
                    "text": self._templates.progress_started,
                },
            ),
            now_ms=now_ms,
        )
        if self._graph_lifecycle is not None:
            await self._graph_lifecycle.prepare(
                run_id=run_id,
                task=task,
                binding=binding,
                definition=self._definition,
                now_ms=now_ms,
            )
        try:
            context = await self._context.collect(
                IMContextRequest(
                    binding["tenant_key"],
                    binding["app_id"],
                    binding["chat_id"],
                    binding["message_id"],
                )
            )
            status = await self._sessions.run_status(run_id)
            outcome = await self._sessions.load_outcome(run_id)
            if outcome is None:
                if status is not None and status is not RunStatus.RUNNING:
                    raise RuntimeError("terminal Agent run is missing its observable outcome")
                outcome = await self._harness.run(
                    HarnessRequest(
                        run_id=run_id,
                        task_id=task.task_id,
                        node_id="/root",
                        user_request=context.trigger.body_text,
                        tool_context=ToolContext(
                            run_id=run_id,
                            node_id="/root",
                            tenant_key=binding["tenant_key"],
                            app_id=binding["app_id"],
                            actor_id=binding["sender_id"],
                            session_key=task.session_key,
                            identity_ref=self._identity_ref,
                            policy_version=self._definition.policy_version,
                            task_id=task.task_id,
                            chat_id=binding["chat_id"],
                            source_message_id=binding["message_id"],
                            reply_in_thread=bool(task.payload.get("thread_id")),
                        ),
                        evidence=(
                            ContextEvidence(
                                source_ref=f"im.message:{context.trigger.message_id}",
                                content="Active request source binding.",
                                source_version=str(context.trigger.source_version_ms),
                            ),
                            *context.evidence,
                        ),
                    ),
                    self._definition,
                    resume=status is RunStatus.RUNNING,
                    now_ms=now_ms,
                )
        except Exception:
            if self._graph_lifecycle is not None and task.attempt_count >= task.max_attempts:
                await self._graph_lifecycle.finish(run_id, RunStatus.FAILED, now_ms=now_ms)
            raise
        if self._graph_lifecycle is not None:
            graph_status = (
                RunStatus.BLOCKED if outcome.status is RunStatus.WAITING else outcome.status
            )
            await self._graph_lifecycle.finish(run_id, graph_status, now_ms=now_ms)
        if context.warnings:
            outcome = AgentOutcome(
                outcome.status,
                outcome.summary,
                outcome.resource_refs,
                (*outcome.warnings, *context.warnings),
            )
        return self._result(task, binding, outcome)

    def failure(self, task: LeasedTask, error: BaseException) -> TaskExecutionResult:
        binding = self._binding(task)
        outcome = AgentOutcome(
            RunStatus.FAILED,
            "The request could not be completed after the retry limit.",
            warnings=(type(error).__name__,),
        )
        return self._result(task, binding, outcome)

    def _result(
        self, task: LeasedTask, binding: dict[str, str], outcome: AgentOutcome
    ) -> TaskExecutionResult:
        task_state = {
            RunStatus.COMPLETED: TaskState.SUCCEEDED,
            RunStatus.BLOCKED: TaskState.BLOCKED,
            RunStatus.WAITING: TaskState.BLOCKED,
            RunStatus.FAILED: TaskState.FAILED,
            RunStatus.CANCELLED: TaskState.CANCELLED,
        }.get(outcome.status)
        if task_state is None:
            raise ValueError("Agent outcome is not terminal")
        message_kind = "completed" if task_state is TaskState.SUCCEEDED else task_state.value
        terminal = OutboxDraft(
            publisher_id="feishu-im.reply",
            destination_ref=binding["message_id"],
            message_kind=message_kind,
            idempotency_key=(
                f"im:{binding['tenant_key']}:{binding['app_id']}:"
                f"{binding['message_id']}:terminal:v1"
            ),
            payload={
                "chat_id": binding["chat_id"],
                "message_id": binding["message_id"],
                "reply_in_thread": bool(task.payload.get("thread_id")),
                "text": self._render(outcome),
            },
        )
        return TaskExecutionResult(
            task_state,
            terminal,
            None if task_state is TaskState.SUCCEEDED else message_kind,
        )

    def _render(self, outcome: AgentOutcome) -> str:
        suffix = {
            RunStatus.COMPLETED: self._templates.completed_suffix,
            RunStatus.BLOCKED: self._templates.blocked_suffix,
            RunStatus.WAITING: self._templates.blocked_suffix,
            RunStatus.FAILED: self._templates.failed_suffix,
            RunStatus.CANCELLED: self._templates.cancelled_suffix,
        }[outcome.status]
        parts = [outcome.summary.strip() or outcome.status.value]
        if outcome.resource_refs:
            parts.append("Resources:\n" + "\n".join(f"- {item}" for item in outcome.resource_refs))
        if outcome.warnings:
            parts.append("Warnings: " + ", ".join(outcome.warnings))
        parts.append(suffix)
        return "\n\n".join(parts)

    @staticmethod
    def _binding(task: LeasedTask) -> dict[str, str]:
        required = ("tenant_key", "app_id", "chat_id", "message_id", "sender_id")
        result: dict[str, str] = {}
        for field in required:
            value = task.payload.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"IM mention task requires {field}")
            result[field] = value
        return result

    @staticmethod
    def run_id_for_task(task_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"codex2lark:im-run:{task_id}"))
