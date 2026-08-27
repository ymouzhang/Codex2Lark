from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from codex2lark.core.budgets import BudgetKind, BudgetLimit
from codex2lark.core.events import LeasedTask
from codex2lark.runtime.context import ContextEngine
from codex2lark.runtime.delegation import (
    AgentMessageTool,
    AgentStatusTool,
    DelegateAgentTool,
    DelegatedHarnessWorker,
    MultiAgentCoordinator,
)
from codex2lark.runtime.harness import AgentHarness
from codex2lark.runtime.multi_agent import GraphStatus, MultiAgentSupervisor
from codex2lark.runtime.resources import ResourceLoader
from codex2lark.runtime.sessions import InMemorySessionStore
from codex2lark.runtime.tools import (
    PolicyDecision,
    ToolContext,
    ToolExecutor,
    ToolReconciliation,
    ToolRegistry,
    WriteScopeTarget,
)
from codex2lark.runtime.types import (
    AgentDefinition,
    AgentOutcome,
    ModelRequest,
    ModelResponse,
    RunStatus,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    VerificationRecord,
    VerificationState,
)
from codex2lark.storage.agent_store import SQLiteAgentGraphStore
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase


class ChildModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse("Primary-source research is complete.")


class Allow:
    async def authorize(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> PolicyDecision:
        del definition, call, context
        return PolicyDecision(True, "test")


class NoApprovals:
    async def request(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> bool:
        del definition, call, context
        return False


class ScopedWriteTool:
    checkpoint_safe_observation = False
    definition = ToolDefinition(
        "scoped.write",
        1,
        "Write one already resolved resource.",
        {
            "type": "object",
            "properties": {"resource": {"type": "string"}},
            "required": ["resource"],
            "additionalProperties": False,
        },
        ToolEffect.WRITE,
    )

    def __init__(self, expected_parallel: int = 2) -> None:
        self.expected_parallel = expected_parallel
        self.active = 0
        self.maximum_active = 0
        self.all_started = asyncio.Event()

    def validate(self, arguments: dict[str, object]) -> None:
        if set(arguments) != {"resource"} or not isinstance(arguments["resource"], str):
            raise ValueError("resource is required")

    async def resolve_delegation_target(
        self, declaration: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        del context
        return WriteScopeTarget("docx", str(declaration["resource"]), "r1")

    async def resolve_write_target(
        self, arguments: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        del context
        return WriteScopeTarget("docx", str(arguments["resource"]), "r1")

    async def execute(
        self, arguments: dict[str, object], context: ToolContext
    ) -> dict[str, object]:
        del context
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == self.expected_parallel:
            self.all_started.set()
        await asyncio.wait_for(self.all_started.wait(), timeout=1)
        self.active -= 1
        return {"resource": {"token": str(arguments["resource"])}}

    async def verify(
        self,
        arguments: dict[str, object],
        observation: dict[str, object],
        context: ToolContext,
    ) -> VerificationRecord:
        del observation, context
        return VerificationRecord(
            VerificationState.VERIFIED,
            "scoped.write.readback",
            "verified",
            (str(arguments["resource"]),),
        )

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        del arguments, context
        return ToolReconciliation(
            {},
            VerificationRecord(VerificationState.UNCERTAIN, "scoped.write", "uncertain"),
        )


class WriterModel:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not any(message.role.value == "tool" for message in request.messages):
            resource = request.messages[-1].content.rsplit(" ", 1)[-1]
            return ModelResponse(
                "",
                (ToolCall(f"write-{resource}", "scoped.write", {"resource": resource}),),
            )
        return ModelResponse("Verified write complete.")


def root_definition() -> AgentDefinition:
    return AgentDefinition(
        "root",
        1,
        "Own the user outcome.",
        "test-model",
        ("agent.delegate",),
        budget_limits=(
            BudgetLimit(BudgetKind.TOOL_CALLS, 16),
            BudgetLimit(BudgetKind.EXTERNAL_WRITES, 6),
        ),
    )


def leased_task() -> LeasedTask:
    return LeasedTask(
        "task-1",
        "event-1",
        "feishu-im",
        "im.handle_mention",
        "tenant/app/chat/message",
        {"request": "Research and summarize the architecture."},
        1,
        3,
        1_000,
    )


async def test_delegate_tool_runs_separate_child_harness_and_returns_typed_artifact(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"g" * 32)))
    supervisor = MultiAgentSupervisor(store)
    sessions = InMemorySessionStore()
    registry = ToolRegistry([])
    model = ChildModel()
    child_harness = AgentHarness(
        model=model,
        tools=registry,
        tool_executor=ToolExecutor(
            registry,
            Allow(),
            NoApprovals(),
            write_scope_store=store,
            clock_ms=lambda: 20,
        ),
        resources=ResourceLoader.from_package("codex2lark.bundled_resources"),
        context=ContextEngine(),
        sessions=sessions,
    )
    coordinator = MultiAgentCoordinator(
        supervisor=supervisor,
        store=store,
        child_harness=child_harness,
        sessions=sessions,
        child_tools=registry,
        model_profile="test-model",
    )
    tool = DelegateAgentTool(coordinator, (), clock_ms=lambda: 20)
    message_tool = AgentMessageTool(coordinator, clock_ms=lambda: 20)
    status_tool = AgentStatusTool(coordinator)
    try:
        await coordinator.prepare(
            run_id="run-root",
            task=leased_task(),
            binding={
                "tenant_key": "tenant",
                "app_id": "app",
                "chat_id": "chat",
                "message_id": "message",
                "sender_id": "user",
            },
            definition=root_definition(),
            now_ms=10,
        )
        arguments = {
            "name": "research",
            "role": "researcher",
            "task_brief": "Find the primary architecture facts.",
            "expected_output_type": "ResearchBundle",
            "tool_ids": [],
            "targets": [],
        }
        context = ToolContext(
            "run-root",
            "/root",
            "tenant",
            "app",
            "user",
            "tenant/app/chat/message",
            "user",
            1,
            "task-1",
        )

        tool.validate(arguments)
        assert tool.parallel_safe_for(arguments) is True
        observation, mail_observation = await asyncio.gather(
            tool.execute(arguments, context),
            message_tool.execute(
                {
                    "child_name": "research",
                    "kind": "steer",
                    "key": "primary-sources",
                    "text": "Focus on the newest authorized primary sources.",
                },
                context,
            ),
        )

        assert observation["artifact_type"] == "ResearchBundle"
        assert observation["verification_state"] == "not_required"
        assert "Primary-source research" in str(observation["artifact"])
        assert mail_observation["kind"] == "steer"
        assert model.requests[0].node_id == "/root/research"
        assert any(
            "newest authorized primary sources" in message.content
            for message in model.requests[0].messages
        )
        status = await status_tool.execute({}, context)
        assert status["children"] == [
            {
                "node_id": observation["node_id"],
                "canonical_path": "/root/research",
                "role": "researcher",
                "status": "completed",
                "artifact_available": True,
            }
        ]
        prepared = await store.find_graph_by_root_run("run-root")
        assert prepared is not None
        nodes = await store.list_nodes(prepared.graph_id)
        assert [node.canonical_path for node in nodes] == ["/root", "/root/research"]

        await coordinator.finish("run-root", RunStatus.COMPLETED, now_ms=30)
        graph = await store.find_graph_by_root_run("run-root")
        assert graph is not None and graph.status is GraphStatus.COMPLETED
    finally:
        await database.close()


def test_delegate_parallel_guard_rejects_writer_children() -> None:
    class GuardCoordinator:
        def tools_are_read_only(self, tool_ids: tuple[str, ...]) -> bool:
            return tool_ids == ("feishu.docs.inspect",)

        def targets_parallel_safe(self, arguments: dict[str, object]) -> bool:
            del arguments
            return False

    tool = DelegateAgentTool(  # type: ignore[arg-type]
        GuardCoordinator(),
        ("feishu.docs.inspect", "feishu.docs.edit"),
    )
    base = {
        "name": "worker",
        "role": "author",
        "task_brief": "Handle one bounded item.",
        "expected_output_type": "Result",
        "targets": [],
    }

    assert tool.parallel_safe_for({**base, "tool_ids": ["feishu.docs.inspect"]}) is True
    assert tool.parallel_safe_for({**base, "tool_ids": ["feishu.docs.edit"]}) is False


async def test_delegate_rejects_writer_without_declared_target(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"u" * 32)))
    registry = ToolRegistry([ScopedWriteTool(expected_parallel=1)])
    coordinator = MultiAgentCoordinator(
        supervisor=MultiAgentSupervisor(store),
        store=store,
        child_harness=None,  # type: ignore[arg-type]
        sessions=InMemorySessionStore(),
        child_tools=registry,
        model_profile="test-model",
    )
    try:
        await coordinator.prepare(
            run_id="run-root",
            task=leased_task(),
            binding={
                "tenant_key": "tenant",
                "app_id": "app",
                "chat_id": "chat",
                "message_id": "message",
                "sender_id": "user",
            },
            definition=root_definition(),
            now_ms=1,
        )
        with pytest.raises(ValueError, match="every delegated writer"):
            await coordinator.delegate(
                {
                    "name": "writer",
                    "role": "author",
                    "task_brief": "Write the target.",
                    "expected_output_type": "OperationResult",
                    "tool_ids": ["scoped.write"],
                    "targets": [],
                },
                ToolContext(
                    "run-root",
                    "/root",
                    "tenant",
                    "app",
                    "user",
                    "tenant/app/chat/message",
                    "user",
                    1,
                    "task-1",
                    "chat",
                ),
                now_ms=2,
            )
    finally:
        await database.close()


async def test_disjoint_live_resolved_writer_children_run_concurrently_with_locks(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"w" * 32)))
    sessions = InMemorySessionStore()
    write_tool = ScopedWriteTool()
    registry = ToolRegistry([write_tool])
    child_harness = AgentHarness(
        model=WriterModel(),
        tools=registry,
        tool_executor=ToolExecutor(
            registry,
            Allow(),
            NoApprovals(),
            write_scope_store=store,
            clock_ms=lambda: 20,
        ),
        resources=ResourceLoader.from_package("codex2lark.bundled_resources"),
        context=ContextEngine(),
        sessions=sessions,
    )
    coordinator = MultiAgentCoordinator(
        supervisor=MultiAgentSupervisor(store),
        store=store,
        child_harness=child_harness,
        sessions=sessions,
        child_tools=registry,
        model_profile="test-model",
    )
    delegate = DelegateAgentTool(coordinator, ("scoped.write",), clock_ms=lambda: 20)
    root = replace(root_definition(), tool_ids=("agent.delegate", "scoped.write"))
    context = ToolContext(
        "run-root",
        "/root",
        "tenant",
        "app",
        "user",
        "tenant/app/chat/message",
        "user",
        1,
        "task-1",
    )
    try:
        await coordinator.prepare(
            run_id="run-root",
            task=leased_task(),
            binding={
                "tenant_key": "tenant",
                "app_id": "app",
                "chat_id": "chat",
                "message_id": "message",
                "sender_id": "user",
            },
            definition=root,
            now_ms=10,
        )

        def arguments(name: str, resource: str) -> dict[str, object]:
            return {
                "name": name,
                "role": "author",
                "task_brief": f"Write {resource}",
                "expected_output_type": "VerifiedWrite",
                "tool_ids": ["scoped.write"],
                "targets": [{"tool_id": "scoped.write", "resource": resource}],
            }

        first = arguments("writer_a", "doc-a")
        second = arguments("writer_b", "doc-b")
        assert delegate.parallel_safe_for(first)
        observations = await asyncio.gather(
            delegate.execute(first, context),
            delegate.execute(second, context),
        )

        assert write_tool.maximum_active == 2
        assert {item["verification_state"] for item in observations} == {"verified"}
        graph = await store.find_graph_by_root_run("run-root")
        assert graph is not None
        nodes = await store.list_nodes(graph.graph_id)
        assert {item.status.value for item in nodes if item.parent_node_id is not None} == {
            "completed"
        }
        lock_count = await database.call(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM runtime_resource_locks"
            ).fetchone()[0]
        )
        assert lock_count == 0
    finally:
        await database.close()


async def test_overlapping_resolved_writer_is_cancelled_before_execution(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"x" * 32)))
    sessions = InMemorySessionStore()
    write_tool = ScopedWriteTool(expected_parallel=2)
    registry = ToolRegistry([write_tool])
    coordinator = MultiAgentCoordinator(
        supervisor=MultiAgentSupervisor(store),
        store=store,
        child_harness=AgentHarness(
            model=WriterModel(),
            tools=registry,
            tool_executor=ToolExecutor(
                registry,
                Allow(),
                NoApprovals(),
                write_scope_store=store,
                clock_ms=lambda: 20,
            ),
            resources=ResourceLoader.from_package("codex2lark.bundled_resources"),
            context=ContextEngine(),
            sessions=sessions,
        ),
        sessions=sessions,
        child_tools=registry,
        model_profile="test-model",
    )
    delegate = DelegateAgentTool(coordinator, ("scoped.write",), clock_ms=lambda: 20)
    root = replace(root_definition(), tool_ids=("agent.delegate", "scoped.write"))
    context = ToolContext(
        "run-root",
        "/root",
        "tenant",
        "app",
        "user",
        "tenant/app/chat/message",
        "user",
        1,
        "task-1",
    )
    try:
        await coordinator.prepare(
            run_id="run-root",
            task=leased_task(),
            binding={
                "tenant_key": "tenant",
                "app_id": "app",
                "chat_id": "chat",
                "message_id": "message",
                "sender_id": "user",
            },
            definition=root,
            now_ms=10,
        )

        def arguments(name: str) -> dict[str, object]:
            return {
                "name": name,
                "role": "author",
                "task_brief": "Write doc-shared",
                "expected_output_type": "VerifiedWrite",
                "tool_ids": ["scoped.write"],
                "targets": [{"tool_id": "scoped.write", "resource": "doc-shared"}],
            }

        first = asyncio.create_task(delegate.execute(arguments("writer_a"), context))
        while write_tool.active == 0:
            await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match="already locked"):
            await delegate.execute(arguments("writer_b"), context)
        write_tool.all_started.set()
        await first

        graph = await store.find_graph_by_root_run("run-root")
        assert graph is not None
        children = [item for item in await store.list_nodes(graph.graph_id) if item.parent_node_id]
        assert {item.spec.name: item.status.value for item in children} == {
            "writer_a": "completed",
            "writer_b": "cancelled",
        }
        assert write_tool.maximum_active == 1
    finally:
        write_tool.all_started.set()
        await database.close()


async def test_prepare_and_finish_are_replay_safe(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"h" * 32)))
    sessions = InMemorySessionStore()
    registry = ToolRegistry([])
    coordinator = MultiAgentCoordinator(
        supervisor=MultiAgentSupervisor(store),
        store=store,
        child_harness=AgentHarness(
            model=ChildModel(),
            tools=registry,
            tool_executor=ToolExecutor(registry, Allow(), NoApprovals()),
            resources=ResourceLoader.from_package("codex2lark.bundled_resources"),
            context=ContextEngine(),
            sessions=sessions,
        ),
        sessions=sessions,
        child_tools=registry,
        model_profile="test-model",
    )
    binding = {
        "tenant_key": "tenant",
        "app_id": "app",
        "chat_id": "chat",
        "message_id": "message",
        "sender_id": "user",
    }
    try:
        await coordinator.prepare(
            run_id="run-root",
            task=leased_task(),
            binding=binding,
            definition=root_definition(),
            now_ms=1,
        )
        await coordinator.prepare(
            run_id="run-root",
            task=leased_task(),
            binding=binding,
            definition=root_definition(),
            now_ms=2,
        )
        await coordinator.finish("run-root", RunStatus.FAILED, now_ms=3)
        await coordinator.finish("run-root", RunStatus.FAILED, now_ms=4)

        graph = await store.find_graph_by_root_run("run-root")
        assert graph is not None
        assert len(await store.list_nodes(graph.graph_id)) == 1
    finally:
        await database.close()


async def test_document_capable_child_artifact_does_not_persist_derived_summary(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"s" * 32)))
    coordinator = MultiAgentCoordinator(
        supervisor=MultiAgentSupervisor(store),
        store=store,
        child_harness=None,  # type: ignore[arg-type]
        sessions=InMemorySessionStore(),
        child_tools=ToolRegistry([]),
        model_profile="test-model",
    )
    try:
        await coordinator.prepare(
            run_id="run-root",
            task=leased_task(),
            binding={
                "tenant_key": "tenant",
                "app_id": "app",
                "chat_id": "chat",
                "message_id": "message",
                "sender_id": "user",
            },
            definition=root_definition(),
            now_ms=1,
        )
        graph = await store.find_graph_by_root_run("run-root")
        assert graph is not None
        root = await store.get_node(graph.root_node_id)
        document_node = replace(
            root,
            spec=replace(root.spec, name="author", tool_ids=("feishu.docs.inspect",)),
        )

        payload = DelegatedHarnessWorker._durable_payload(
            document_node,
            AgentOutcome(
                RunStatus.COMPLETED,
                "Sensitive document-derived summary",
                ("https://example.feishu.cn/docx/docx_1",),
            ),
        )

        assert "Sensitive document-derived summary" not in str(payload)
        assert payload["content_refetch_required"] is True
        assert payload["resource_refs"] == ["https://example.feishu.cn/docx/docx_1"]
    finally:
        await database.close()
