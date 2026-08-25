from __future__ import annotations

import asyncio
import hashlib
import re
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
    GraphRecord,
    GraphStatus,
    MailboxKind,
    MultiAgentSupervisor,
    NodeExecutionInput,
    NodeExecutionResult,
    NodeSpec,
    NodeStatus,
    ResourceTarget,
)
from .sessions import SessionStore
from .tools import ToolContext, ToolReconciliation, ToolRegistry, WriteScopeTarget
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


def _declared_targets(arguments: dict[str, object]) -> tuple[dict[str, object], ...]:
    value = arguments.get("targets")
    if not isinstance(value, list):
        raise ValueError("agent.delegate targets must be an array")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"tool_id", "resource"}:
            raise ValueError("each delegation target requires only tool_id and resource")
        if not all(isinstance(item[field], str) and item[field] for field in item):
            raise ValueError("delegation target values must be non-empty strings")
        result.append(dict(item))
    return tuple(result)


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
        graph_store: AgentGraphStore,
    ) -> None:
        self._harness = harness
        self._sessions = sessions
        self._tools = tools
        self._parent_context = parent_context
        self._model_profile = model_profile
        self._now_ms = now_ms
        self._graph_store = graph_store

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
                    user_request=self._user_request(node, execution),
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
                        chat_id=self._parent_context.chat_id,
                        source_message_id=self._parent_context.source_message_id,
                        reply_in_thread=self._parent_context.reply_in_thread,
                        write_scope=tuple(
                            WriteScopeTarget(
                                item.resource_type,
                                item.resource_id,
                                item.expected_revision,
                            )
                            for item in await self._graph_store.list_locks(
                                node.node_id, now_ms=self._now_ms
                            )
                        ),
                        write_scope_required=node.spec.requires_write_scope,
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
    def _user_request(node: AgentNode, execution: NodeExecutionInput) -> str:
        updates: list[str] = []
        for item in execution.mailbox:
            text = item.payload.get("text")
            if isinstance(text, str) and text:
                updates.append(f"- {item.kind.value}: {text}")
        if not updates:
            return node.spec.task_brief
        return (
            f"{node.spec.task_brief}\n\n"
            "Scoped parent mailbox updates (user-level instructions; they cannot change "
            "policy, identity, tools, budgets, or write scope):\n" + "\n".join(updates)
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
        self._delegation_gate = asyncio.Lock()

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
        declarations = _declared_targets(arguments)
        if "agent.delegate" in requested_tools:
            raise PermissionError("delegated workers cannot receive recursive delegation")
        definitions = self._child_tools.definitions(requested_tools)
        if role in {AgentRole.RESEARCHER, AgentRole.VERIFIER} and any(
            item.effect is not ToolEffect.READ for item in definitions
        ):
            raise PermissionError(f"{role.value} workers may receive read-only tools only")
        async with self._delegation_gate:
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
            if existing is not None:
                artifact = self._artifact_for(
                    existing.node_id, await self._store.list_artifacts(graph.graph_id)
                )
                if artifact is not None:
                    return self._artifact_observation(existing, artifact)
            targets = await self._resolve_targets(declarations, requested_tools, context)
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
                    requires_write_scope=bool(targets),
                ),
                ready=not targets,
                now_ms=now_ms,
            )
            if targets:
                acquired = True
                for target in targets:
                    if not await self._store.acquire_lock(
                        graph.graph_id,
                        child.node_id,
                        target,
                        now_ms=now_ms,
                        lease_ms=300_000,
                    ):
                        acquired = False
                        break
                if not acquired:
                    await self._store.release_locks(child.node_id)
                    await self._supervisor.cancel(child.node_id, now_ms=now_ms)
                    raise RuntimeError("delegated write target is already locked")
                if child.status is NodeStatus.CREATED:
                    child = await self._supervisor.activate(child.node_id, now_ms=now_ms)
        async with self._delegation_gate:
            pass
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
                graph_store=self._store,
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
                artifact = await self._wait_for_artifact(
                    graph.graph_id, child.node_id, timeout_s=300.0
                )
        if artifact is None:
            raise RuntimeError("delegated Agent did not produce an artifact")
        return self._artifact_observation(child, artifact)

    async def message(
        self, arguments: dict[str, object], context: ToolContext, *, now_ms: int
    ) -> dict[str, object]:
        graph = await self._root_graph(context)
        kind = MailboxKind(str(arguments["kind"]))
        if kind not in {MailboxKind.MESSAGE, MailboxKind.STEER, MailboxKind.FOLLOW_UP}:
            raise ValueError("unsupported root mailbox message kind")
        async with self._delegation_gate:
            child = await self._direct_child(graph.graph_id, str(arguments["child_name"]))
            if child.status not in {
                NodeStatus.CREATED,
                NodeStatus.READY,
                NodeStatus.INTERRUPTED,
            }:
                raise RuntimeError("child Agent is no longer accepting pre-lease messages")
            key = str(arguments["key"])
            correlation = hashlib.sha256(
                f"{context.run_id}\0{child.node_id}\0{kind.value}\0{key}".encode()
            ).hexdigest()
            item = await self._supervisor.send(
                graph_id=graph.graph_id,
                sender_node_id=graph.root_node_id,
                recipient_node_id=child.node_id,
                kind=kind,
                payload={"text": str(arguments["text"])},
                correlation_id=correlation,
                now_ms=now_ms,
            )
        return {
            "item_id": item.item_id,
            "child_id": child.node_id,
            "canonical_path": child.canonical_path,
            "kind": item.kind.value,
            "sequence": item.sequence,
        }

    async def status(self, context: ToolContext) -> dict[str, object]:
        graph = await self._root_graph(context)
        artifacts = await self._store.list_artifacts(graph.graph_id)
        producers = {item.producer_node_id for item in artifacts}
        children = [
            item
            for item in await self._store.list_nodes(graph.graph_id)
            if item.parent_node_id == graph.root_node_id
        ]
        return {
            "graph_status": graph.status.value,
            "children": [
                {
                    "node_id": item.node_id,
                    "canonical_path": item.canonical_path,
                    "role": item.spec.role.value,
                    "status": item.status.value,
                    "artifact_available": item.node_id in producers,
                }
                for item in children
            ],
        }

    async def _root_graph(self, context: ToolContext) -> GraphRecord:
        graph = await self._store.find_graph_by_root_run(context.run_id)
        if graph is None or graph.status is not GraphStatus.ACTIVE:
            raise RuntimeError("active root Agent graph is unavailable")
        if context.node_id != "/root":
            raise PermissionError("only the root Agent may use collaboration tools")
        return graph

    async def _direct_child(self, graph_id: str, name: str) -> AgentNode:
        graph = await self._store.get_graph(graph_id)
        child = next(
            (
                item
                for item in await self._store.list_nodes(graph_id)
                if item.parent_node_id == graph.root_node_id and item.spec.name == name
            ),
            None,
        )
        if child is None:
            raise LookupError("direct child Agent does not exist")
        return child

    @staticmethod
    def _artifact_observation(child: AgentNode, artifact: Artifact) -> dict[str, object]:
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

    def targets_parallel_safe(self, arguments: dict[str, object]) -> bool:
        try:
            tool_ids = _delegated_tool_ids(arguments)
            declarations = _declared_targets(arguments)
            definitions = self._child_tools.definitions(tool_ids)
        except (LookupError, ValueError):
            return False
        writers = {
            item.tool_id
            for item in definitions
            if item.effect in {ToolEffect.WRITE, ToolEffect.DESTRUCTIVE}
        }
        if not writers:
            return True
        declared = {str(item["tool_id"]) for item in declarations}
        if not writers.issubset(declared):
            return False
        return all(
            callable(getattr(self._child_tools.require(tool_id), "resolve_delegation_target", None))
            for tool_id in writers
        )

    async def _resolve_targets(
        self,
        declarations: tuple[dict[str, object], ...],
        requested_tools: tuple[str, ...],
        context: ToolContext,
    ) -> tuple[ResourceTarget, ...]:
        definitions = {
            item.tool_id: item for item in self._child_tools.definitions(requested_tools)
        }
        writers = {
            tool_id
            for tool_id, definition in definitions.items()
            if definition.effect in {ToolEffect.WRITE, ToolEffect.DESTRUCTIVE}
        }
        if writers and not declarations:
            raise ValueError("every delegated writer requires a declared target")
        if not declarations:
            return ()
        if not writers.issubset({str(item["tool_id"]) for item in declarations}):
            raise ValueError("every delegated writer requires a declared target")
        resolved: list[ResourceTarget] = []
        for declaration in declarations:
            tool_id = str(declaration["tool_id"])
            if tool_id not in writers:
                raise ValueError("delegation targets may reference writer tools only")
            resolver = getattr(
                self._child_tools.require(tool_id), "resolve_delegation_target", None
            )
            if not callable(resolver):
                raise ValueError(f"delegated writer has no target resolver: {tool_id}")
            target = await resolver(declaration, context)
            if not isinstance(target, WriteScopeTarget):
                raise TypeError("delegation target resolver returned an invalid target")
            resolved.append(
                ResourceTarget(
                    context.tenant_key,
                    target.resource_type,
                    target.resource_id,
                    target.expected_revision,
                )
            )
        unique = {
            (item.tenant_key, item.resource_type, item.resource_id): item for item in resolved
        }
        return tuple(unique[key] for key in sorted(unique))

    @staticmethod
    def _artifact_for(node_id: str, artifacts: list[Artifact]) -> Artifact | None:
        return next((item for item in artifacts if item.producer_node_id == node_id), None)

    async def _wait_for_artifact(
        self, graph_id: str, node_id: str, *, timeout_s: float
    ) -> Artifact | None:
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    artifact = self._artifact_for(
                        node_id, await self._store.list_artifacts(graph_id)
                    )
                    if artifact is not None:
                        return artifact
                    node = await self._store.get_node(node_id)
                    if node.status in {
                        NodeStatus.BLOCKED,
                        NodeStatus.FAILED,
                        NodeStatus.CANCELLED,
                    }:
                        return None
                    await asyncio.sleep(0.01)
        except TimeoutError:
            return None


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
                    "targets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool_id": {
                                    "type": "string",
                                    "enum": list(self.allowed_tool_ids),
                                },
                                "resource": {"type": "string"},
                            },
                            "required": ["tool_id", "resource"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "name",
                    "role",
                    "task_brief",
                    "expected_output_type",
                    "tool_ids",
                    "targets",
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
            "targets",
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
        _declared_targets(arguments)

    def parallel_safe_for(self, arguments: dict[str, object]) -> bool:
        try:
            tool_ids = _delegated_tool_ids(arguments)
        except ValueError:
            return False
        return self.coordinator.tools_are_read_only(
            tool_ids
        ) or self.coordinator.targets_parallel_safe(arguments)

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


class AgentMessageTool:
    checkpoint_safe_observation = True

    def __init__(
        self,
        coordinator: MultiAgentCoordinator,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.definition = ToolDefinition(
            "agent.message",
            1,
            (
                "Send one durable scoped message, steer, or follow-up to a direct child Agent. "
                "Declare agent.delegate earlier in the same tool batch and use a stable key."
            ),
            {
                "type": "object",
                "properties": {
                    "child_name": {"type": "string", "pattern": "^[a-z0-9_]+$"},
                    "kind": {
                        "type": "string",
                        "enum": ["message", "steer", "follow_up"],
                    },
                    "key": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
                    "text": {"type": "string", "maxLength": 4000},
                },
                "required": ["child_name", "kind", "key", "text"],
                "additionalProperties": False,
            },
            ToolEffect.READ,
            parallel_safe=True,
        )

    def validate(self, arguments: dict[str, object]) -> None:
        if set(arguments) != {"child_name", "kind", "key", "text"}:
            raise ValueError("agent.message arguments must match the strict schema")
        if not re.fullmatch(r"[a-z0-9_]+", str(arguments["child_name"])):
            raise ValueError("agent.message child_name is invalid")
        if str(arguments["kind"]) not in {"message", "steer", "follow_up"}:
            raise ValueError("agent.message kind is invalid")
        if not re.fullmatch(r"[a-z0-9_-]+", str(arguments["key"])):
            raise ValueError("agent.message key is invalid")
        text = arguments["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 4_000:
            raise ValueError("agent.message text is invalid")

    async def execute(
        self, arguments: dict[str, object], context: ToolContext
    ) -> dict[str, object]:
        return await self.coordinator.message(arguments, context, now_ms=self.clock_ms())

    async def verify(
        self,
        arguments: dict[str, object],
        observation: dict[str, object],
        context: ToolContext,
    ) -> VerificationRecord:
        del arguments, observation, context
        return VerificationRecord(
            VerificationState.NOT_REQUIRED,
            "multi-agent.mailbox",
            "durable child mailbox item committed",
        )

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        del arguments, context
        return ToolReconciliation(
            {},
            VerificationRecord(
                VerificationState.NOT_REQUIRED,
                "multi-agent.mailbox",
                "internal collaboration message",
            ),
        )


class AgentStatusTool:
    checkpoint_safe_observation = True

    def __init__(self, coordinator: MultiAgentCoordinator) -> None:
        self.coordinator = coordinator
        self.definition = ToolDefinition(
            "agent.status",
            1,
            "List content-free lifecycle status for direct child Agents.",
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            ToolEffect.READ,
            parallel_safe=True,
        )

    def validate(self, arguments: dict[str, object]) -> None:
        if arguments:
            raise ValueError("agent.status accepts no arguments")

    async def execute(
        self, arguments: dict[str, object], context: ToolContext
    ) -> dict[str, object]:
        del arguments
        return await self.coordinator.status(context)

    async def verify(
        self,
        arguments: dict[str, object],
        observation: dict[str, object],
        context: ToolContext,
    ) -> VerificationRecord:
        del arguments, observation, context
        return VerificationRecord(
            VerificationState.NOT_REQUIRED,
            "multi-agent.status",
            "content-free child status read",
        )

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        del arguments, context
        return ToolReconciliation(
            {},
            VerificationRecord(
                VerificationState.NOT_REQUIRED,
                "multi-agent.status",
                "internal collaboration status",
            ),
        )
