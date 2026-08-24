from __future__ import annotations

from pathlib import Path

from codex2lark.core.budgets import BudgetKind, BudgetLimit
from codex2lark.core.events import LeasedTask
from codex2lark.runtime.context import ContextEngine
from codex2lark.runtime.delegation import DelegateAgentTool, MultiAgentCoordinator
from codex2lark.runtime.harness import AgentHarness
from codex2lark.runtime.multi_agent import GraphStatus, MultiAgentSupervisor
from codex2lark.runtime.resources import ResourceLoader
from codex2lark.runtime.sessions import InMemorySessionStore
from codex2lark.runtime.tools import PolicyDecision, ToolContext, ToolExecutor, ToolRegistry
from codex2lark.runtime.types import (
    AgentDefinition,
    ModelRequest,
    ModelResponse,
    RunStatus,
    ToolCall,
    ToolDefinition,
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


def root_definition() -> AgentDefinition:
    return AgentDefinition(
        "root",
        1,
        "Own the user outcome.",
        "test-model",
        ("agent.delegate",),
        budget_limits=(
            BudgetLimit(BudgetKind.MODEL_TOKENS, 32_000),
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
        tool_executor=ToolExecutor(registry, Allow(), NoApprovals()),
        resources=ResourceLoader([]),
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
        observation = await tool.execute(arguments, context)

        assert observation["artifact_type"] == "ResearchBundle"
        assert observation["verification_state"] == "not_required"
        assert "Primary-source research" in str(observation["artifact"])
        assert model.requests[0].node_id == "/root/research"
        prepared = await store.find_graph_by_root_run("run-root")
        assert prepared is not None
        nodes = await store.list_nodes(prepared.graph_id)
        assert [node.canonical_path for node in nodes] == ["/root", "/root/research"]

        await coordinator.finish("run-root", RunStatus.COMPLETED, now_ms=30)
        graph = await store.find_graph_by_root_run("run-root")
        assert graph is not None and graph.status is GraphStatus.COMPLETED
    finally:
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
            resources=ResourceLoader([]),
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
