from __future__ import annotations

import time
from collections.abc import Callable

from codex2lark.core.budgets import BudgetKind, BudgetLimit
from codex2lark.core.events import LeasedTask

from .harness import AgentHarness, HarnessRequest
from .multi_agent import (
    AgentGraphStore,
    AgentNode,
    AgentRole,
    Artifact,
    ArtifactDraft,
    ContextMode,
    GraphLimits,
    GraphStatus,
    MultiAgentSupervisor,
    NodeExecutionInput,
    NodeExecutionResult,
    NodeSpec,
)
from .sessions import SessionStore
from .tools import ToolContext, ToolReconciliation, ToolRegistry
from .types import (
    AgentDefinition,
    AgentOutcome,
    RunStatus,
    ToolDefinition,
    ToolEffect,
    VerificationRecord,
    VerificationState,
)


def _delegated_tool_ids(arguments: dict[str, object]) -> tuple[str, ...]:
    value = arguments.get("tool_ids")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("agent.delegate tool_ids must be an array of strings")
    return tuple(value)


class DelegatedHarnessWorker:
    def __init__(
        self,
        *,
        harness: AgentHarness,
        sessions: SessionStore,
        tools: ToolRegistry,
        parent_context: ToolContext,
        model_profile: str,
        now_ms: int,
    ) -> None:
        self._harness = harness
        self._sessions = sessions
        self._tools = tools
        self._parent_context = parent_context
        self._model_profile = model_profile
        self._now_ms = now_ms

    async def execute(self, execution: NodeExecutionInput) -> NodeExecutionResult:
        node = execution.node
        if self._parent_context.task_id is None:
            raise ValueError("delegated Agent requires the parent task ID")
        status = await self._sessions.run_status(node.node_id)
        outcome = await self._sessions.load_outcome(node.node_id)
        definitions = self._tools.definitions(node.spec.tool_ids)
        requires_verification = any(
            item.effect in {ToolEffect.WRITE, ToolEffect.DESTRUCTIVE} for item in definitions
        )
        if outcome is None:
            if status is not None and status is not RunStatus.RUNNING:
                raise RuntimeError("terminal child Agent is missing its outcome")
            definition = AgentDefinition(
                agent_id=f"feishu-{node.spec.role.value}",
                version=1,
                instructions=self._instructions(node),
                model_profile=self._model_profile,
                tool_ids=node.spec.tool_ids,
                resource_packages=("delegated-worker-core",),
                budget_limits=tuple(
                    BudgetLimit(BudgetKind(key), value)
                    for key, value in node.spec.budgets.items()
                    if key in {item.value for item in BudgetKind}
                ),
                max_turns=6,
                max_context_tokens=16_000,
                require_verified_external_effect=requires_verification,
            )
            outcome = await self._harness.run(
                HarnessRequest(
                    run_id=node.node_id,
                    task_id=self._parent_context.task_id,
                    node_id=node.canonical_path,
                    user_request=node.spec.task_brief,
                    tool_context=ToolContext(
                        run_id=node.node_id,
                        node_id=node.canonical_path,
                        tenant_key=self._parent_context.tenant_key,
                        app_id=self._parent_context.app_id,
                        actor_id=self._parent_context.actor_id,
                        session_key=self._parent_context.session_key,
                        identity_ref=self._parent_context.identity_ref,
                        policy_version=self._parent_context.policy_version,
                        task_id=self._parent_context.task_id,
                    ),
                ),
                definition,
                resume=status is RunStatus.RUNNING,
                now_ms=self._now_ms,
            )
        if outcome.status is not RunStatus.COMPLETED:
            raise RuntimeError(f"delegated Agent ended as {outcome.status.value}")
        return NodeExecutionResult(
            ArtifactDraft(
                artifact_type=node.spec.expected_output_type,
                payload=self._durable_payload(node, outcome),
                source_versions={},
                verification_state=(
                    VerificationState.VERIFIED
                    if requires_verification
                    else VerificationState.NOT_REQUIRED
                ),
            ),
            acknowledged_mail_ids=tuple(item.item_id for item in execution.mailbox),
        )

    @staticmethod
    def _durable_payload(node: AgentNode, outcome: AgentOutcome) -> dict[str, object]:
        summary = outcome.summary
        refs = list(outcome.resource_refs)
        warnings = list(outcome.warnings)
        has_feishu_business_access = any(
            tool_id.startswith("feishu.") for tool_id in node.spec.tool_ids
        )
        if has_feishu_business_access:
            return {
                "claims": {node.spec.name: {"status": "completed"}},
                "resource_refs": refs,
                "warnings": warnings,
                "content_refetch_required": True,
            }
        return {
            "claims": {node.spec.name: summary},
            "summary": summary,
            "resource_refs": refs,
            "warnings": warnings,
        }

    @staticmethod
    def _instructions(node: AgentNode) -> str:
        return (
            f"You are the {node.spec.role.value} worker at {node.canonical_path}. "
            "Complete only the assigned bounded deliverable. Use only enabled tools, never "
            "claim unverified writes, and return a concise evidence-based result to the parent."
        )


class MultiAgentCoordinator:
    def __init__(
        self,
        *,
        supervisor: MultiAgentSupervisor,
        store: AgentGraphStore,
        child_harness: AgentHarness,
        sessions: SessionStore,
        child_tools: ToolRegistry,
        model_profile: str,
    ) -> None:
        self._supervisor = supervisor
        self._store = store
        self._child_harness = child_harness
        self._sessions = sessions
        self._child_tools = child_tools
        self._model_profile = model_profile

    async def prepare(
        self,
        *,
        run_id: str,
        task: LeasedTask,
        binding: dict[str, str],
        definition: AgentDefinition,
        now_ms: int,
    ) -> None:
        if await self._store.find_graph_by_root_run(run_id) is not None:
            return
        await self._supervisor.create_graph(
            root_run_id=run_id,
            tenant_key=binding["tenant_key"],
            app_id=binding["app_id"],
            source_resource_kind="im.message",
            source_resource_id=binding["message_id"],
            agent_definition_id=definition.agent_id,
            agent_definition_version=definition.version,
            root_spec=NodeSpec(
                name="root",
                role=AgentRole.ORCHESTRATOR,
                task_brief=str(task.payload.get("request", "Own the user outcome.")),
                expected_output_type="AgentOutcome",
                tool_ids=definition.tool_ids,
                budgets={item.kind.value: item.maximum for item in definition.budget_limits},
                context_mode=ContextMode.SELECTED,
            ),
            limits=GraphLimits(),
            now_ms=now_ms,
        )

    async def finish(self, run_id: str, status: RunStatus, *, now_ms: int) -> None:
        graph = await self._store.find_graph_by_root_run(run_id)
        if graph is None:
            raise LookupError("root Agent graph is unavailable")
        if graph.status is not GraphStatus.ACTIVE:
            return
        if status is RunStatus.CANCELLED:
            await self._supervisor.cancel(graph.root_node_id, now_ms=now_ms)
            return
        await self._supervisor.publish_terminal(
            graph.graph_id, graph.root_node_id, status, now_ms=now_ms
        )

    async def delegate(
        self, arguments: dict[str, object], context: ToolContext, *, now_ms: int
    ) -> dict[str, object]:
        graph = await self._store.find_graph_by_root_run(context.run_id)
        if graph is None or graph.status is not GraphStatus.ACTIVE:
            raise RuntimeError("active root Agent graph is unavailable")
        if context.node_id != "/root":
            raise PermissionError("only the root Agent may use agent.delegate")
        role = AgentRole(str(arguments["role"]))
        requested_tools = _delegated_tool_ids(arguments)
        if "agent.delegate" in requested_tools:
            raise PermissionError("delegated workers cannot receive recursive delegation")
        definitions = self._child_tools.definitions(requested_tools)
        if role in {AgentRole.RESEARCHER, AgentRole.VERIFIER} and any(
            item.effect is not ToolEffect.READ for item in definitions
        ):
            raise PermissionError(f"{role.value} workers may receive read-only tools only")
        root = await self._store.get_node(graph.root_node_id)
        name = str(arguments["name"])
        existing = next(
            (
                item
                for item in await self._store.list_nodes(graph.graph_id)
                if item.parent_node_id == root.node_id and item.spec.name == name
            ),
            None,
        )
        child = existing or await self._supervisor.spawn(
            graph.graph_id,
            root.node_id,
            NodeSpec(
                name=name,
                role=role,
                task_brief=str(arguments["task_brief"]),
                expected_output_type=str(arguments["expected_output_type"]),
                tool_ids=requested_tools,
                budgets={
                    "model_tokens": 12_000,
                    "tool_calls": 6,
                    "external_writes": 2,
                },
                context_mode=ContextMode.SELECTED,
            ),
            now_ms=now_ms,
        )
        artifact = self._artifact_for(
            child.node_id, await self._store.list_artifacts(graph.graph_id)
        )
        if artifact is None:
            worker = DelegatedHarnessWorker(
                harness=self._child_harness,
                sessions=self._sessions,
                tools=self._child_tools,
                parent_context=context,
                model_profile=self._model_profile,
                now_ms=now_ms,
            )
            batch = await self._supervisor.execute_ready(
                graph.graph_id,
                worker_id=f"delegate:{context.run_id}",
                worker=worker,
                now_ms=now_ms,
                lease_ms=300_000,
            )
            if child.node_id in batch.failed_node_ids:
                raise RuntimeError("delegated Agent failed")
            artifact = self._artifact_for(child.node_id, list(batch.completed_artifacts))
        if artifact is None:
            raise RuntimeError("delegated Agent did not produce an artifact")
        return {
            "node_id": child.node_id,
            "canonical_path": child.canonical_path,
            "artifact_type": artifact.artifact_type,
            "verification_state": artifact.verification_state.value,
            "artifact": artifact.payload,
        }

    def tools_are_read_only(self, tool_ids: tuple[str, ...]) -> bool:
        return all(
            definition.effect is ToolEffect.READ
            for definition in self._child_tools.definitions(tool_ids)
        )

    @staticmethod
    def _artifact_for(node_id: str, artifacts: list[Artifact]) -> Artifact | None:
        return next((item for item in artifacts if item.producer_node_id == node_id), None)


class DelegateAgentTool:
    checkpoint_safe_observation = False

    def __init__(
        self,
        coordinator: MultiAgentCoordinator,
        allowed_tool_ids: tuple[str, ...],
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.allowed_tool_ids = allowed_tool_ids
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.definition = ToolDefinition(
            "agent.delegate",
            1,
            (
                "Delegate one concrete independent deliverable to a bounded worker Agent and "
                "return its typed artifact. Do not delegate trivial or sequential work."
            ),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z0-9_]+$"},
                    "role": {
                        "type": "string",
                        "enum": ["researcher", "author", "data_analyst", "verifier"],
                    },
                    "task_brief": {"type": "string"},
                    "expected_output_type": {"type": "string"},
                    "tool_ids": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(self.allowed_tool_ids)},
                    },
                },
                "required": [
                    "name",
                    "role",
                    "task_brief",
                    "expected_output_type",
                    "tool_ids",
                ],
                "additionalProperties": False,
            },
            ToolEffect.READ,
            parallel_safe=True,
        )

    def validate(self, arguments: dict[str, object]) -> None:
        if set(arguments) != {
            "name",
            "role",
            "task_brief",
            "expected_output_type",
            "tool_ids",
        }:
            raise ValueError("agent.delegate arguments must match the strict schema")
        NodeSpec(
            name=str(arguments["name"]),
            role=AgentRole(str(arguments["role"])),
            task_brief=str(arguments["task_brief"]),
            expected_output_type=str(arguments["expected_output_type"]),
            tool_ids=_delegated_tool_ids(arguments),
            budgets={},
        )
        if not set(_delegated_tool_ids(arguments)).issubset(self.allowed_tool_ids):
            raise PermissionError("delegated tool IDs exceed the configured allowlist")

    def parallel_safe_for(self, arguments: dict[str, object]) -> bool:
        try:
            tool_ids = _delegated_tool_ids(arguments)
        except ValueError:
            return False
        return self.coordinator.tools_are_read_only(tool_ids)

    async def execute(
        self, arguments: dict[str, object], context: ToolContext
    ) -> dict[str, object]:
        return await self.coordinator.delegate(arguments, context, now_ms=self.clock_ms())

    async def verify(
        self,
        arguments: dict[str, object],
        observation: dict[str, object],
        context: ToolContext,
    ) -> VerificationRecord:
        del arguments, context
        state = VerificationState(str(observation["verification_state"]))
        return VerificationRecord(
            state=state,
            verifier_id="multi-agent.artifact",
            summary=f"delegated artifact is {state.value}",
        )

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        del arguments, context
        return ToolReconciliation(
            {},
            VerificationRecord(
                VerificationState.NOT_REQUIRED,
                "multi-agent.artifact",
                "delegation is not an external write",
            ),
        )
